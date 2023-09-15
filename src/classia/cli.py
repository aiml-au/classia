import argparse
import logging
import json
import os

from classia.dataset import hierarchy_and_labels_from_folder
from .train import train_model, write_data, get_dataset, get_dataloader, prepare_dataloaders, evaluate
from .predict import predict_images, predict_docs
from .models import *

import torch
from fs import open_fs
logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)

REMOTE_FS_URL = os.getenv("REMOTE_FS", default="gs://aiml-shop-classia-public") 

def run():
    global REMOTE_FS_URL

    parser = argparse.ArgumentParser(description="A hierarchical classifier")

    parser.add_argument("--datasets_dir", help="The path of the directory to store named datasets",
                        default=os.path.expanduser("~/.cache/classia/datasets"))
    parser.add_argument("--models_dir", help="The path of the directory to store named models",
                        default=os.path.expanduser("~/.cache/classia/models"))
    
    parser.add_argument("--image_model_size", help="The size variant for the Image model: (small, medium, large)", default="medium")
    parser.add_argument("--text_model_size", help="The size variant for the Text model: (base, large)", default="base")

    subparsers = parser.add_subparsers(dest="command")

    train_parser = subparsers.add_parser("train", help="Train a new hierarchical classification model")
    train_parser.add_argument("--model", help="The name of the model", type=str, required=True)
    train_parser.add_argument("--epochs", help="The number of epochs to train for", type=int, default=10)
    train_parser.add_argument("--batch_size", help="The batch size to use during training", type=int, default=8)
    train_parser.add_argument("--lr", help="The learning rate to use during training", type=float, default=0.001)
    train_parser.add_argument("--resume", help="Resume training from the last best epoch", type=bool, default=False)
    train_datasource_group = train_parser.add_mutually_exclusive_group()
    train_datasource_group.add_argument("--images",
                                        help="The directory of training images, if training an image classifier")
    train_datasource_group.add_argument("--docs",
                                        help="The directory of training documents, if training a text classifier")

    test_parser = subparsers.add_parser("test", help="Evaluate a hierarchical classification model using a test set")
    test_parser.add_argument("--model", help="The name of the model", required=True)
    test_parser.add_argument("--batch_size", help="The batch size to use for evaluation dataloaders", type=int, default=8)
    test_datasource_group = test_parser.add_mutually_exclusive_group()
    test_datasource_group.add_argument("--images",
                                       help="The directory of testing images, if testing an image classifier")
    test_datasource_group.add_argument("--docs",
                                       help="The directory of testing documents, if testing a text classifier")

    predict_parser = subparsers.add_parser("predict",
                                           help="Use a trained hierarchical classification model on unlabelled examples")
    predict_parser.add_argument("--model", help="The name of the model", required=True)
    predict_parser.add_argument("files", nargs="+", help="The filenames of the examples to classify.")

    download_parser = subparsers.add_parser("download", help="Download pre-trained model weights")
    download_parser.add_argument("--model", help="The name of the model")
    download_parser.add_argument("--dataset", help="The name of the dataset")
    download_parser.add_argument("--download_dir", help="The path of the directory to download named models",
                        default=os.path.expanduser("~/.cache/classia"))
    

    export_parser = subparsers.add_parser("export", help="Export pre-trained model weights")
    export_parser.add_argument("--model", help="The name of the model")
    export_parser.add_argument("--export_dir", help="The path of the directory to export models",
                        default=os.path.expanduser("~/.cache/classia"))
    export_parser.add_argument("--docs", help="The directory of training documents, if training a text classifier")
    export_parser.add_argument("--images", help="The directory of training images, if training an image classifier")
    export_parser.add_argument("--batch_size", help="The batch size to use during training", type=int, default=8)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()

    elif args.command == "train":        
        os.makedirs(f'{args.models_dir}/{args.model}', exist_ok=True)
        model_type = None

        if args.images:           
            model_type = 'image'
            model_size = args.image_model_size            
            tree, label_set, files, labels = hierarchy_and_labels_from_folder(args.images)
            model= get_image_model(tree, args.image_model_size)
        elif args.docs:
            model_type = 'text'
            model_size = args.text_model_size
            tree, label_set, files, labels = hierarchy_and_labels_from_folder(args.docs)
            model= get_text_model(tree, args.text_model_size)
        else:
            train_parser.error("One of --images or --docs must be provided.")

        if model_type:       
            meta_data = {"model_type": model_type, "model_size": model_size, "model_weights": model._get_name()}
            write_data(tree, label_set, files, labels, meta_data, models_dir=args.models_dir, model_name=args.model)
            dataset = get_dataset(model_type, files, labels, model_size=model_size)
            train_loader, eval_loader = prepare_dataloaders(model_type, dataset, batch_size=args.batch_size, model_size=model_size)
            train_model(model, train_loader, eval_loader, tree, label_set, models_dir=args.models_dir, model_name=args.model, model_type=model_type, model_size=model_size, epochs=args.epochs, lr=args.lr, resume=args.resume)

    elif args.command == "test":      

        if os.path.exists(f'{args.models_dir}/{args.model}/best.pth'):
            checkpoint = torch.load(f'{args.models_dir}/{args.model}/best.pth')
            model_name = checkpoint['model_weight_name']
            model_size = checkpoint['model_size']
            model_type = checkpoint['model_type']
            tree = checkpoint['tree']
            model = eval(model_name)
            model = model(tree)
            model.load_state_dict(checkpoint['model_state_dict'])
        else:
            LOGGER.warning('Saved best model does not exist in this directory.')
            return
        

        if args.images:
            if model_type == 'text':
                LOGGER.info(f"Pretrained model type is {model_type} but arg --images was passed. Pass --docs with this model type.")
                return

            LOGGER.info(f"Test {args.model} on {args.images}")            
            tree, _, files, labels = hierarchy_and_labels_from_folder(args.images)

        elif args.docs:
            if model_type == 'image':
                LOGGER.info(f"Pretrained model type is {model_type} but arg --docs was passed. Pass --images with this model type.")
                return
    
            LOGGER.info(f"Test {args.model} on {args.docs}")
            tree, _, files, labels = hierarchy_and_labels_from_folder(args.docs)

        else:
            test_parser.error("One of --images or --docs must be provided.")

        eval_dataset = get_dataset(model_type, files, labels, model_size=model_size)
        eval_loader = get_dataloader(model_type, eval_dataset, batch_size=args.batch_size, model_size=model_size)
        evaluate(model, eval_loader, tree)

    elif args.command == "predict":

        if not os.path.exists(f'{args.models_dir}/{args.model}/best.pth'):
            predict_parser.error(f"Model {args.model} does not exist ({args.models_dir}/{args.model})")
        else:
            checkpoint = torch.load(f'{args.models_dir}/{args.model}/best.pth')

            if checkpoint['model_type'] == "image":
                predict_images(args.files, checkpoint)
            elif checkpoint['model_type'] == "text":
                predict_docs(args.files, checkpoint)
            else:
                predict_parser.error(f"Unknown model type {checkpoint['model_type']}")

    elif args.command == "download":

        if args.model:   
            download_dir = f"{args.download_dir}/models/{args.model}" if ".cache" in args.download_dir else f"{args.download_dir}/{args.model}"
            REMOTE_FS_URL += f"/models/{args.model}" 
            model_weights = "best.pth" # ["best.pth", "latest.pth"]
            download('Model', REMOTE_FS_URL, model_weights, download_dir)

        elif args.dataset:
            download_dir = f"{args.download_dir}/datasets/{args.dataset}" if ".cache" in args.download_dir else f"{args.download_dir}/{args.dataset}"
            REMOTE_FS_URL += f"/datasets"
            dataset_name = args.dataset if ".zip" in args.dataset else f"{args.dataset}.zip"
            download('Dataset', REMOTE_FS_URL, dataset_name, download_dir)
        else:            
            download_parser.error("One of --model or --dataset must be provided.")

    elif args.command == "export": 
        checkpoint = torch.load(f"{args.models_dir}/{args.model}/best.pth") 
        model_type = checkpoint['model_type']
        model_name = checkpoint['model_weight_name']
        tree = checkpoint['tree']

        model = eval(model_name) # TODO: check if this way of loading model is safe
        model = model(tree)
        model.load_state_dict(checkpoint['model_state_dict'])
        device='cuda'

        if model_type == "image": # torch.Size([8, 3, 224, 224])
            input_shape = (3, 224, 224)
            dummy_input = torch.randn(args.batch_size, *input_shape).to(device)
        elif model_type == "text": # torch.Size([8, 256])
            input_shape = 256
            dummy_input = torch.randn(args.batch_size, input_shape).to(device)
        else:
            export_parser.error(f"Unknown model type {model_type}")

        export(model, dummy_input)


def export(model, dummy_input, device='cuda'):
    model.to(device)
    model.eval()
    dummy_input = dummy_input.long()

    # print('dummy_input', dummy_input.shape)
    # Testing against text model gives following error. May be worth trying model.half or float.
    # RuntimeError: CUDA error: CUBLAS_STATUS_NOT_INITIALIZED when calling `cublasCreate(handle)`
    torch.onnx.export(model,
                  dummy_input,
                  "classia.onnx",
                  input_names=['input'],
                  output_names=['output'],
                  opset_version=11)
    

def get_model_from_checkpoint(models_dir, model):
    if os.path.exists(f'{models_dir}/{model}/best.pth'):
        checkpoint = torch.load(f'{models_dir}/{model}/best.pth')
        model_name = checkpoint['model_weight_name']
        model = eval(model_name)
        model.load_state_dict(checkpoint['model_state_dict'])
    else:
        LOGGER.warning('Saved best model does not exist in this directory.')
        return

    return model
    

def download(flag, remote_fs_url, file, download_dir):            
    fs = open_fs(remote_fs_url)

    if not fs.exists(file):
        LOGGER.warning(
            f"{flag}: {file} does not exist in the remote location."
        )
    else:          
        os.makedirs(download_dir, exist_ok=True)
        loca_target_file = f'{download_dir}/{file}'

        if not os.path.exists(loca_target_file):
            LOGGER.info(f"Downloading {file}")
        else:
            LOGGER.warning(f"Overriding {file} in {download_dir} by downloading from remote location.")

        with open(loca_target_file, "wb") as f:
            fs.download(file, f) 

        LOGGER.info(f'{flag} was successfully downloaded in {loca_target_file}')
        
