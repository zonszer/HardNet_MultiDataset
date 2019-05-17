# -*- coding: utf-8 -*-
"""
This is HardNet local patch descriptor. The training code is based on PyTorch TFeat implementation
https://github.com/edgarriba/examples/tree/master/triplet
by Edgar Riba.

If you use this code, please cite:

@article{HardNet2017,
    author = {Anastasiya Mishchuk, Dmytro Mishkin, Filip Radenovic, Jiri Matas},
    title = "{Working hard to know your neighbor's margins:Local descriptor learning loss}",
    year = 2017}
(c) 2017 by Anastasiia Mishchuk, Dmytro Mishkin

@article{HardNetAMOS2019,
    author = {Milan Pultar, Dmytro Mishkin, Jiri Matas},
    title = "{Leveraging Outdoor Webcams for Local Descriptor Learning}",
    year = 2019,
    month = feb,
    booktitle = {Proceedings of CVWW 2019}
}
"""
from __future__ import division, print_function
import torchvision.transforms as transforms
import torch.backends.cudnn as cudnn
from Utils import *
from HandCraftedModules import get_WF_from_string
from os import path
from io_helpers import send_email, get_last_checkpoint
import argparse, tqdm, PIL, cv2, os, pickle
from Dataset import TripletPhotoTour
from Losses import *
from Dataset import *
from architectures import *
from Learning import *
from EvalMetrics import *
from Losses import loss_HardNetMulti


parser = argparse.ArgumentParser(description="PyTorch HardNet")
parser.add_argument("--model-dir", default="models/", help="folder to output model checkpoints")
parser.add_argument("--name", default="", help="Experiment name prefix")

parser.add_argument("--loss", default="triplet_margin", help="Other options: softmax, contrastive")
parser.add_argument("--batch-reduce", default="min", help="Other options: average, random, random_global, L2Net")
parser.add_argument("--resume", default="", type=str, metavar="PATH", help="path to latest checkpoint (default: none)")
parser.add_argument("--start-epoch", default=0, type=int, metavar="N", help="manual epoch number (useful on restarts)")
parser.add_argument("--epochs", type=int, default=10, metavar="E", help="number of epochs to train (default: 10)")
parser.add_argument("--batch-size", type=int, default=1024, metavar="BS", help="input batch size for training (default: 1024)")
parser.add_argument("--test-batch-size", type=int, default=128, metavar="BST", help="input batch size for testing (default: 1024)")
parser.add_argument("--n-triplets", type=int, default=5000000, metavar="N", help="how many tuples will generate from the dataset")
parser.add_argument("--margin", type=float, default=1.0, metavar="MARGIN", help="the margin value for the triplet loss function (default: 1.0")
parser.add_argument("--lr", type=float, default=20.0, metavar="LR", help="learning rate (default: 10.0)")
parser.add_argument("--fliprot", type=str2bool, default=True, help="turns on flip and 90deg rotation augmentation")
parser.add_argument("--wd", default=1e-4, type=float, metavar="W", help="weight decay (default: 1e-4)")
parser.add_argument("--optimizer", default="sgd", type=str, metavar="OPT", help="The optimizer to use (default: SGD)")
parser.add_argument("--n-patch-sets", type=int, default=30000, help="How many patch sets to generate. 300k is ~ 6000 per image seq for HPatches")
parser.add_argument("--id", type=int, default=0, help="experiment id")

parser.add_argument("--seed", type=int, default=0, metavar="S", help="random seed (default: 0)")
parser.add_argument("--log-interval", type=int, default=1, metavar="LI", help="how many batches to wait before logging training status")

parser.add_argument("--cams-in-batch", type=int, default=0, help="how many cams are source ones for a batch in AMOS")

parser.add_argument("--patch-gen", type=str, default="oneRes", help="options: oneImg, sumImg")
parser.add_argument("--PS", default=False, action="store_true", help="options: use HardNetPS model")
parser.add_argument("--debug", default=False, action="store_true", help="verbal")

parser.add_argument("--older", type=str2bool, default=False, help="For use with old torchvision")

parser.add_argument(
    "--weight-function",
    type=str,
    default="Hessian",
    help="Keypoints are generated with probability ~ weight function. If None (default), then uniform sampling. Variants: Hessian, HessianSqrt, HessianSqrt4, None",
)
args = parser.parse_args()

txt = []
txt += ["PS:" + str(args.n_patch_sets) + "PP"]
txt += ["WF:" + args.weight_function]
txt += ["PG:" + args.patch_gen]
split_name = "_".join(txt)

txt = []
txt += ["id:" + str(args.id)]
txt += ["TrS:" + args.name]
txt += ["loss:" + args.loss.replace('_','')]
txt += [split_name]
txt += [args.batch_reduce]
txt += ["tps:" + str(args.n_triplets)]
txt += ["camsB:" + str(args.cams_in_batch)]
txt += ["ep:" + str(args.epochs)]
save_name = "_".join([str(c) for c in txt])


cudnn.benchmark = True
torch.cuda.manual_seed_all(args.seed)
torch.manual_seed(args.seed)
np.random.seed(args.seed)


# resize image to size 32x32
cv2_scale = lambda x: cv2.resize(x, dsize=(32, 32), interpolation=cv2.INTER_LINEAR)
# reshape image
np_reshape32 = lambda x: np.reshape(x, (32, 32, 1))
np_reshape64 = lambda x: np.reshape(x, (64, 64, 1))

# np_reshape64 = lambda x: np.reshape(x, (64, 64, 1))
transform_test = transforms.Compose([
            transforms.ToPILImage(),
            transforms.Resize(32),
            transforms.ToTensor()])

transform_train_1 = transforms.Compose(
        [
            transforms.ToPILImage(),
            transforms.RandomAffine(25, scale=(0.8, 1.4), shear=25, resample=PIL.Image.BICUBIC),
            transforms.RandomResizedCrop(32, scale=(0.7, 1.0), ratio=(0.9, 1.10)),
            transforms.ToTensor(),
        ]
    )


default_transform = {"default": transform_train_1}
# easy_transform={'e1':t, 'e2':t, 'e3':t, 'default':transform}

transform_AMOS = transforms.Compose(
    [
        transforms.ToPILImage(),
        transforms.RandomAffine(25, scale=(0.8, 1.4), shear=25, resample=PIL.Image.BICUBIC),
        transforms.CenterCrop(64),
        transforms.RandomResizedCrop(32, scale=(0.7, 1.0), ratio=(0.9, 1.10)),
        transforms.ToTensor(),
    ]
)

if args.older: # older torchvision does not run transforms above
    def transform_test(img):
        img = (img.numpy()) / 255.0
        return transforms.Compose([
            transforms.Lambda(cv2_scale),
            transforms.Lambda( lambda x: np.reshape(x, (32, 32, 1)) ),
            transforms.ToTensor(),
        ])(img)
    def t(img):
        img = transforms.Compose([
            transforms.Lambda( lambda x: np.reshape(x, (1, 64, 64)) ),
            transforms.ToPILImage(),
            transforms.RandomAffine(25, scale=(0.8, 1.4), shear=25, resample=PIL.Image.BICUBIC),
            transforms.RandomResizedCrop(32, scale=(0.7, 1.0), ratio=(0.9, 1.10)),
            transforms.ToTensor(),
        ])(img)
        return img.type(torch.float64)
    default_transform={'default':transform_test}
    # easy_transform={'e1':t, 'e2':t, 'e3':t, 'default':transform}
    transform_train_1 = default_transform
    transform_AMOS = transforms.Compose([transforms.ToPILImage(),
                                         transforms.RandomAffine(25, scale=(0.8, 1.4), shear=25, resample=PIL.Image.BICUBIC),
                                         transforms.CenterCrop(64),
                                         transforms.RandomResizedCrop(32, scale=(0.7, 1.0), ratio=(0.9, 1.10)),
                                         transforms.ToTensor()])


def get_test_loaders():
    kwargs = {"num_workers": 4, "pin_memory": True}
    test_loaders = [
        {
            "name": name,
            "dataloader": torch.utils.data.DataLoader(
                TripletPhotoTour(train=False, batch_size=args.test_batch_size, n_triplets=1000,
                                 root=path.join("Datasets"), name=name, download=True, transform=transform_test),
                batch_size=args.test_batch_size,
                shuffle=False,
                **kwargs
            ),
        }
        for name in ["liberty", "notredame", "yosemite", "liberty_harris", "notredame_harris", "yosemite_harris"]
    ]

    return test_loaders



def test(test_loader, model, epoch, logger_test_name, args):
    model.eval()
    labels, distances = [], []
    mean_losses = []
    pbar = tqdm(enumerate(test_loader))
    for batch_idx, (data_a, data_p, label) in pbar:
        data_a = data_a.cuda()
        data_p = data_p.cuda()
        out_a =  model(data_a)
        out_p = model(data_p)
        dists = torch.sqrt(torch.sum((out_a - out_p) ** 2, 1))  # euclidean distance
        distances.append(dists.data.cpu().numpy().reshape(-1, 1))
        labels.append(label.data.cpu().numpy().reshape(-1, 1))
        if batch_idx % args.log_interval == 0:
            pbar.set_description(logger_test_name + " Test Epoch: {} [{}/{} ({:.0f}%)]".format(epoch,
                                                                                               batch_idx * len(data_a), 
                                                                                               len(test_loader.dataset),
                                                                                               100.0 * batch_idx / len(test_loader)))
    num_tests = test_loader.dataset.matches.size(0)
    labels = np.vstack(labels).reshape(num_tests)
    distances = np.vstack(distances).reshape(num_tests)
    fpr95 = ErrorRateAt95Recall(labels, 1.0 / (distances + 1e-8))
    print("\33[91mTest set: FPR95: {:.8f}\33[0m".format(fpr95))
    print("\33[91mTest set: AP: {:.8f}\33[0m".format(AP(labels, distances)))
        

def train(train_loader, model, optimizer, epoch):
    model.train()
    train_loader.prepare_epoch()
    pbar = tqdm(enumerate(train_loader))
    for batch_idx, data in pbar:
        data_a, data_p = data
        data_a = data_a.cuda()
        data_p = data_p.cuda()
        out_a = model(data_a)
        out_p = model(data_p)
        loss = loss_HardNetMulti(out_a, out_p,
                            margin=args.margin,
                            anchor_swap=True,
                            batch_reduce=args.batch_reduce, 
                            loss_type=args.loss)
        loss = loss.mean()
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        success = adjust_learning_rate(optimizer, args.lr, args.batch_size, args.n_triplets, args.epochs)
        if success < 0:  # just to be sure - never ascend
            break
        if batch_idx % args.log_interval == 0:
            pbar.set_description(
                "Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}".format(epoch, 
                                                                         batch_idx * len(data_a), 
                                                                         len(train_loader) * len(data_a), 
                                                                         100.0 * batch_idx / len(train_loader),
                                                                         loss.item())
            )

    os.makedirs(os.path.join(args.model_dir, save_name), exist_ok=True)
    save_path = os.path.join(args.model_dir, save_name, "checkpoint_{}.pth".format(epoch))
    torch.save({"epoch": epoch + 1, "state_dict": model.state_dict()}, save_path)
    print("saving to: {}".format(save_path))
    print_lr(optimizer)
    return


def print_lr(optimizer):
    for group in optimizer.param_groups:
        print("Learning rate: " + str(group["lr"]))
        return


def main(train_loader, test_loaders, model):
    print("\nparsed options:\n{}\n".format(vars(args)))
    model.cuda()
    optimizer1 = create_optimizer(model, args.lr, args.optimizer, args.wd)
    if args.resume:  # optionally resume from a checkpoint
        p1 = args.resume
        p2 = os.path.join(args.model_dir, args.resume)
        path_resume = None
        if os.path.isfile(p1): # try the path as absolute filepath first
            path_resume = p1
        elif os.path.isfile(p2): # then try it as filepath in model_dir
            path_resume = p2
        elif os.path.exists(p2): # finally try it as dir name in model_dir (picks latest checkpoint)
            print("searching dir")
            path_resume = os.path.join(p2, get_last_checkpoint(p2))
        if path_resume is not None:
            print("=> loading checkpoint {}".format(path_resume))
            if args.PS:
                model = HardNetPS()
                checkpoint = torch.load(path_resume)
                model.load_state_dict(checkpoint)
                model.cuda()
            else:
                checkpoint = torch.load(path_resume)
                try:
                    args.start_epoch = checkpoint["epoch"]
                    model.load_state_dict(checkpoint["state_dict"])
                except:
                    print("loading subset of weights")
                    aux = model.state_dict()
                    aux.update(checkpoint)
                    model.load_state_dict(aux)
                try:
                    optimizer1.load_state_dict(checkpoint["optimizer"])
                except:
                    print("optimizer not loaded")
        else:
            print("=> no checkpoint found")

    start = args.start_epoch
    end = start + args.epochs
    for epoch in range(start, end):
        train(train_loader, model, optimizer1, epoch)
        for test_loader in test_loaders:
            test(test_loader["dataloader"], model, epoch, test_loader["name"], args)


if __name__ == "__main__":
    tst = get_test_loaders()
    DSs = []
    DSs += [DS_Brown("Datasets/liberty.pt", True, default_transform)]
    DSs += [DS_Brown('Datasets/liberty_harris.pt', True, default_transform)]
    DSs += [DS_Brown('Datasets/notredame.pt', True, default_transform)]
    DSs += [DS_Brown('Datasets/notredame_harris.pt', True, default_transform)]
    DSs += [DS_Brown('Datasets/yosemite.pt', True, default_transform)]
    DSs += [DS_Brown('Datasets/yosemite_harris.pt', True, default_transform)]
    DSs += [DS_Brown('Datasets/hpatches_split_view_train.pt', True, default_transform)]
    DSs += [DS_AMOS('Datasets/AMOS_views_v3/Train', split_name, args.n_patch_sets, get_WF_from_string(args.weight_function), True, transform_AMOS,
                    args.patch_gen, args.cams_in_batch, masks_dir='Datasets/AMOS_views_v3/Masks')]

    wrapper = DS_wrapper(DSs, args.n_triplets, args.batch_size, frequencies=[1,1,1,1,1,1,6,6])
    os.makedirs(os.path.join(args.model_dir, save_name), exist_ok=True)
    with open(os.path.join(args.model_dir, save_name, "setup.txt"), "w") as f:
        for d in wrapper.datasets:
            print(d.__dict__, file=f)

    print("----------------\nsplit_name: {}".format(split_name))
    print("save_name: {}".format(save_name))
    model = HardNet().cuda()
    main(wrapper, tst, model)
    print("Train end, saved: {}".format(save_name))
    # send_email(recipient='milan.pultar@gmail.com', ignore_host='milan-XPS-15-9560') # useful for training, change the recipient address for yours or comment this out
