import torch
import torch.optim as optim
import torch.optim.lr_scheduler as lr_sched
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.autograd import Variable
import numpy as np
import os
from torchvision import transforms
from models import RSCNN_SSN_Cls as RSCNN_SSN
from data import ModelNet40Cls
import utils.pytorch_utils as pt_utils
import utils.pointnet2_utils as pointnet2_utils
import data.data_utils as d_utils
import my_point_utils as point_utils
import argparse
import random
import yaml

torch.backends.cudnn.enabled = True
torch.backends.cudnn.benchmark = True
torch.backends.cudnn.deterministic = True

seed = 123
random.seed(seed)
np.random.seed(seed)
torch.manual_seed(seed)            
torch.cuda.manual_seed(seed)       
torch.cuda.manual_seed_all(seed) 

parser = argparse.ArgumentParser(description='Relation-Shape CNN Shape Classification Training')
parser.add_argument('--config', default='cfgs/config_ssn_cls.yaml', type=str)

def main():
    args = parser.parse_args()
    with open(args.config) as f:
        config = yaml.load(f)
    print("\n**************************")
    for k, v in config['common'].items():
        setattr(args, k, v)
        print('\n[%s]:'%(k), v)
    print("\n**************************\n")
    
    try:
        os.makedirs(args.save_path)
    except OSError:
        pass
    
    train_transforms = transforms.Compose([
        d_utils.PointcloudToTensor(),
        d_utils.PointcloudScaleAndTranslate(),
        d_utils.PointcloudRandomInputDropout()
    ])
    test_transforms = transforms.Compose([
        d_utils.PointcloudToTensor(),
        #d_utils.PointcloudScaleAndTranslate()
    ])
    
    train_dataset = ModelNet40Cls(num_points = args.num_points, root = args.data_root, transforms=train_transforms)
    train_dataloader = DataLoader(
        train_dataset, 
        batch_size=args.batch_size,
        shuffle=True, 
        num_workers=int(args.workers)
    )

    test_dataset = ModelNet40Cls(num_points = args.num_points, root = args.data_root, transforms=test_transforms, train=False)
    test_dataloader = DataLoader(
        test_dataset, 
        batch_size=args.batch_size,
        shuffle=False, 
        num_workers=int(args.workers)
    )
    
    model = RSCNN_SSN(num_classes = args.num_classes, input_channels = args.input_channels, relation_prior = args.relation_prior, use_xyz = True)
    # for multi GPU
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    if torch.cuda.is_available() and torch.cuda.device_count()>=2:
        model = nn.DataParallel(model, device_ids=[0, 1])
        model.to(device)
    elif  torch.cuda.is_available() and torch.cuda.device_count()==1:
        model.cuda()

    optimizer = optim.Adam(
        model.parameters(), lr=args.base_lr, weight_decay=args.weight_decay)

    lr_lbmd = lambda e: max(args.lr_decay**(e // args.decay_step), args.lr_clip / args.base_lr)
    bnm_lmbd = lambda e: max(args.bn_momentum * args.bn_decay**(e // args.decay_step), args.bnm_clip)
    lr_scheduler = lr_sched.LambdaLR(optimizer, lr_lbmd)
    bnm_scheduler = pt_utils.BNMomentumScheduler(model, bnm_lmbd)
    
    if args.checkpoint is not '':
        model.load_state_dict(torch.load(args.checkpoint))
        print('Load model successfully: %s' % (args.checkpoint))

    criterion = nn.CrossEntropyLoss()
    num_batch = len(train_dataset)/args.batch_size
    
    # training
    train(train_dataloader, test_dataloader, model, criterion, optimizer, lr_scheduler, bnm_scheduler, args, num_batch)
    

def train(train_dataloader, test_dataloader, model, criterion, optimizer, lr_scheduler, bnm_scheduler, args, num_batch):
    #PointcloudScaleAndTranslate = d_utils.PointcloudScaleAndTranslate()   # initialize augmentation
    global g_acc 
    g_acc = 0.91    # only save the model whose acc > 0.91
    batch_count = 0
    model.train()
    for epoch in range(args.epochs):
        for i, data in enumerate(train_dataloader, 0):
            #if lr_scheduler is not None:
            #    lr_scheduler.step(epoch)
            #if bnm_scheduler is not None:
            #    bnm_scheduler.step(epoch-1)
            points, target = data
            points, target = points.cuda(), target.cuda()
            #points, target = Variable(points), Variable(target)
            
            # fastest point sampling
            fps_idx = point_utils.farthest_point_sampling(points.permute(0,2,1), args.num_points)  # (B, npoint)
            #fps_idx = fps_idx[:, np.random.choice(1200, args.num_points, False)]
            points = point_utils.index_points(points.transpose(1, 2).contiguous(), fps_idx).transpose(1, 2).contiguous()  # (B, N, 3)
            
            # augmentation
            #points = PointcloudScaleAndTranslate(points.detach())
            
            optimizer.zero_grad()
            
            pred = model(points)
            target = target.view(-1)
            loss = criterion(pred, target)
            loss.backward()

            optimizer.step()

            if i % args.print_freq_iter == 0:
                print('[epoch %3d: %3d/%3d] \t train loss: %0.6f \t lr: %0.5f' %(epoch+1, i, num_batch, loss.item(), lr_scheduler.get_lr()[0]))
            batch_count += 1

            
            # validation in between an epoch
            if args.evaluate and batch_count % int(args.val_freq_epoch * num_batch) == 0:
                validate(test_dataloader, model, criterion, args, batch_count)

        if lr_scheduler is not None:
            lr_scheduler.step(epoch)


def validate(test_dataloader, model, criterion, args, iter): 
    global g_acc
    model.eval()
    print("now evaluate...")
    losses, preds, labels = [], [], []
    with torch.no_grad():
        #model.eval()
        for j, data in enumerate(test_dataloader, 0):
            points, target = data
            points, target = points.cuda(), target.cuda()
            #points, target = Variable(points), Variable(target)
            
            # fastest point sampling
            fps_idx = point_utils.farthest_point_sampling(points.permute(0,2,1).contiguous(), args.num_points)  # (B, npoint)
            #fps_idx = fps_idx[:, np.random.choice(1200, args.num_points, False)]
            points = point_utils.index_points(points.transpose(1, 2).contiguous(), fps_idx).transpose(1, 2).contiguous()

            #print(torch.sum(torch.isnan(points)))
            pred = model(points)
            #print(torch.isnan(pred)[0,0])
            target = target.view(-1)

            loss = criterion(pred, target)
            losses.append(loss.item())
            _, pred_choice = torch.max(pred, -1)

            
            preds.append(pred_choice)
            labels.append(target)
            
        preds = torch.cat(preds, 0)
        labels = torch.cat(labels, 0)
        #print(torch.sum(preds == labels), labels.numel())
        acc = torch.sum(preds == labels).item()/labels.numel()
        print('\nval loss: %0.6f \t acc: %0.6f\n' %(np.array(losses).mean(), acc))
        if acc > g_acc:
            g_acc = acc
            torch.save(model.state_dict(), '%s/cls_ssn_iter_%d_acc_%0.6f.pth' % (args.save_path, iter, acc))
        model.train()
    
if __name__ == "__main__":
    main()