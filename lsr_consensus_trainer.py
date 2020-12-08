'''
This is a plain consensus version modification on trainer.py
'''

import argparse
import asyncio
import os
import pickle
import sys
import time

import resnet
import torch
import torch.backends.cudnn as cudnn
import torch.nn as nn
import torch.nn.parallel
import torch.optim
import torch.utils.data
import torchvision.datasets as datasets
import torchvision.transforms as transforms

sys.path.append('./distributed-learning/')

from model_statistics import ModelStatistics
from utils.consensus_tcp import ConsensusAgent

model_names = sorted(name for name in resnet.__dict__
                     if name.islower() and not name.startswith("__")
                     and name.startswith("resnet")
                     and callable(resnet.__dict__[name]))

parser = argparse.ArgumentParser(description='Proper ResNets for CIFAR10 in pytorch')
parser.add_argument('--arch', '-a', metavar='ARCH', default='resnet20',
                    choices=model_names,
                    help='model architecture: ' + ' | '.join(model_names) +
                         ' (default: resnet20)')

# Arguments for consensus:
parser.add_argument('--agent-token', '-t', required=True, type=int)
parser.add_argument('--agent-host', default='127.0.0.1', type=str)
parser.add_argument('--agent-port', required=True, type=int)
parser.add_argument('--init-leader', dest='init_leader', action='store_true')
parser.add_argument('--master-host', default='127.0.0.1', type=str)
parser.add_argument('--master-port', required=True, type=int)
parser.add_argument('--enable-log', dest='logging', action='store_true')
parser.add_argument('--total-agents', required=True, type=int)
parser.add_argument('--debug-consensus', dest='debug', action='store_true')
parser.add_argument('--target-split', dest='target_split', action='store_true')

parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--epochs', default=200, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=32, type=int,
                    metavar='N', help='mini-batch size (default: 32)')
parser.add_argument('--lr', '--learning-rate', default=0.1, type=float,
                    metavar='LR', help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum')
parser.add_argument('--weight-decay', '--wd', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)')
parser.add_argument('--print-freq', '-p', default=50, type=int,
                    metavar='N', help='print frequency (default: 50)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
parser.add_argument('--pretrained', dest='pretrained', action='store_true',
                    help='use pre-trained model')
parser.add_argument('--half', dest='half', action='store_true',
                    help='use half-precision(16-bit) ')
parser.add_argument('--save-dir', dest='save_dir',
                    help='The directory used to save the trained models',
                    default='save_temp', type=str)
parser.add_argument('--save-every', dest='save_every',
                    help='Saves checkpoints at every specified number of epochs',
                    type=int, default=10)
best_prec1 = 0


async def main():
    global args, best_prec1
    args = parser.parse_args()

    torch.manual_seed(239)

    print('Consensus agent: {}'.format(args.agent_token))
    convergence_eps = 1e-4
    agent = ConsensusAgent(args.agent_token, args.agent_host, args.agent_port, args.master_host, args.master_port,
                           convergence_eps=convergence_eps, debug=True if args.debug else False)
    agent_serve_task = asyncio.create_task(agent.serve_forever())
    print('{}: Created serving task'.format(args.agent_token))

    # Check the save_dir exists or not
    args.save_dir = os.path.join(args.save_dir, str(args.agent_token))
    if not os.path.exists(args.save_dir):
        os.makedirs(args.save_dir)

    model = torch.nn.DataParallel(resnet.__dict__[args.arch]())
    model.cuda()

    statistics = ModelStatistics(args.agent_token)

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            if args.logging:
                print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']
            best_prec1 = checkpoint['best_prec1']
            if 'statistics' in checkpoint.keys():
                statistics = pickle.loads(checkpoint['statistics'])
            elif os.path.isfile(os.path.join(args.resume, 'statistics.pickle')):
                statistics = ModelStatistics.load_from_file(os.path.join(args.resume, 'statistics.pickle'))
            model.load_state_dict(checkpoint['state_dict'])
            if args.logging:
                print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.evaluate, checkpoint['epoch']))
        else:
            if args.logging:
                print("=> no checkpoint found at '{}'".format(args.resume))

    cudnn.benchmark = True

    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])

    dataset_path = os.path.join('./data/', str(args.agent_token))
    train_dataset = datasets.CIFAR10(root=dataset_path, train=True, transform=transforms.Compose([
        transforms.RandomHorizontalFlip(),
        transforms.RandomCrop(32, 4),
        transforms.ToTensor(),
        normalize,
    ]), download=True)

    size_per_agent = len(train_dataset) // args.total_agents
    train_indices = list(
        range(args.agent_token * size_per_agent, min(len(train_dataset), (args.agent_token + 1) * size_per_agent)))

    if args.target_split:
        train_indices = list(range(len(train_dataset)))[train_dataset.targets == args.agent_token]
        print('Target split: {} samples for agent {}'.format(len(train_indices), args.agent_token))

    from torch.utils.data.sampler import SubsetRandomSampler
    train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=args.batch_size, shuffle=False,  # !!!!!
        num_workers=args.workers, pin_memory=True,
        sampler=SubsetRandomSampler(train_indices)
    )

    val_loader = torch.utils.data.DataLoader(
        datasets.CIFAR10(root=dataset_path, train=False, transform=transforms.Compose([
            transforms.ToTensor(),
            normalize,
        ])),
        batch_size=128, shuffle=False,
        num_workers=args.workers, pin_memory=True)

    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().cuda()

    if args.half:
        model.half()
        criterion.half()

    optimizer = torch.optim.SGD(model.parameters(), args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)

    def lr_schedule(epoch):
        factor = args.total_agents
        if epoch >= 81:
            factor /= 10
        if epoch >= 122:
            factor /= 10
        return factor

    lr_scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda=lr_schedule)

    if args.arch != 'resnet20':
        print('This code was not intended to be used on resnets other than resnet20')

    if args.arch in ['resnet1202', 'resnet110']:
        # for resnet1202 original paper uses lr=0.01 for first 400 minibatches for warm-up
        # then switch back. In this setup it will correspond for first epoch.
        for param_group in optimizer.param_groups:
            param_group['lr'] = args.lr * 0.1

    if args.evaluate:
        validate(val_loader, model, criterion)
        return

    def dump_params(model):
        return torch.cat([v.to(torch.float32).view(-1) for k, v in model.state_dict().items()]).cpu().numpy()

    def load_params(model, params):
        st = model.state_dict()
        used_params = 0
        for k in st.keys():
            cnt_params = st[k].numel()
            st[k] = torch.Tensor(params[used_params:used_params + cnt_params]).view(st[k].shape)\
                .to(st[k].dtype).to(st[k].device)
            used_params += cnt_params
        model.load_state_dict(st)

    async def run_averaging():
        params = dump_params(model)
        params = await agent.run_once(params)
        load_params(model, params)

    if args.logging:
        print('Starting initial averaging...')

    params = dump_params(model)
    params = await agent.run_round(params, 1.0 if args.init_leader else 0.0)
    load_params(model, params)

    if args.logging:
        print('Initial averaging completed!')

    for epoch in range(args.start_epoch, args.epochs):
        statistics.set_epoch(epoch)
        # train for one epoch
        if args.logging:
            print('current lr {:.5e}'.format(optimizer.param_groups[0]['lr']))
        statistics.add('train_begin_timestamp', time.time())
        await train(train_loader, model, criterion, optimizer, epoch, statistics, run_averaging)
        lr_scheduler.step()
        statistics.add('train_end_timestamp', time.time())

        # evaluate on validation set
        statistics.add('validate_begin_timestamp', time.time())
        prec1 = validate(val_loader, model, criterion)
        statistics.add('validate_end_timestamp', time.time())
        statistics.add('val_precision', prec1)

        # remember best prec@1 and save checkpoint
        is_best = prec1 > best_prec1
        best_prec1 = max(prec1, best_prec1)

        if epoch > 0 and epoch % args.save_every == 0:
            save_checkpoint({
                'epoch': epoch + 1,
                'state_dict': model.state_dict(),
                'best_prec1': best_prec1,
                'statistics': pickle.dumps(statistics)
            }, is_best, filename=os.path.join(args.save_dir, 'checkpoint.th'))

        save_checkpoint({
            'state_dict': model.state_dict(),
            'best_prec1': best_prec1,
        }, is_best, filename=os.path.join(args.save_dir, 'model.th'))
        statistics.dump_to_file(os.path.join(args.save_dir, 'statistics.pickle'))


async def train(train_loader, model, criterion, optimizer, epoch, statistics, run_averaging):
    """
        Run one train epoch
    """
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()

    # switch to train mode
    model.train()

    start = time.time()
    end = time.time()
    for i, (input, target) in enumerate(train_loader):

        # measure data loading time
        data_time.update(time.time() - end)

        target = target.cuda()
        input_var = input.cuda()
        target_var = target
        if args.half:
            input_var = input_var.half()

        # compute output
        output = model(input_var)
        loss = criterion(output, target_var)

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # average model
        await run_averaging()

        output = output.float()
        loss = loss.float()
        # measure accuracy and record loss
        prec1 = accuracy(output.data, target)[0]
        losses.update(loss.item(), input.size(0))
        top1.update(prec1.item(), input.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            if args.logging:
                print('\rEpoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})'.format(
                epoch, i, len(train_loader), batch_time=batch_time,
                data_time=data_time, loss=losses, top1=top1), end='')
    if args.logging:
        print('\nEpoch took {:.2f} s.'.format(end - start))
    statistics.add('train_precision', top1.avg)
    statistics.add('train_loss', losses.avg)


def validate(val_loader, model, criterion):
    """
    Run evaluation
    """
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()

    # switch to evaluate mode
    model.eval()

    end = time.time()
    with torch.no_grad():
        for i, (input, target) in enumerate(val_loader):
            target = target.cuda()
            input_var = input.cuda()
            target_var = target.cuda()

            if args.half:
                input_var = input_var.half()

            # compute output
            output = model(input_var)
            loss = criterion(output, target_var)

            output = output.float()
            loss = loss.float()

            # measure accuracy and record loss
            prec1 = accuracy(output.data, target)[0]
            losses.update(loss.item(), input.size(0))
            top1.update(prec1.item(), input.size(0))

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if i % args.print_freq == 0:
                if args.logging:
                    print('\rTest: [{0}/{1}]\t'
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                      'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                      'Prec@1 {top1.val:.3f} ({top1.avg:.3f})'.format(
                    i, len(val_loader), batch_time=batch_time, loss=losses,
                    top1=top1), end='')

    if args.logging:
        print('\n * Prec@1 {top1.avg:.3f}'
          .format(top1=top1))

    return top1.avg


def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    """
    Save the training model
    """
    torch.save(state, filename)


class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


if __name__ == '__main__':
    asyncio.get_event_loop().run_until_complete(main())
