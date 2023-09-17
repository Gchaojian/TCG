"""
Training scripts.

"""
import os
import yaml
import torch
import logging
import argparse
import warnings
import numpy as np
import torch.nn as nn
from tqdm import tqdm
from utils.logger import ColoredLogger
from utils.builder import ConfigBuilder
from utils.constants import LOSS_INF
from utils.functions import display_results, to_device
from time import perf_counter


logging.setLoggerClass(ColoredLogger)
logger = logging.getLogger(__name__)
warnings.simplefilter("ignore", UserWarning)

parser = argparse.ArgumentParser()
parser.add_argument(
    '--cfg', '-c', 
    default = os.path.join('configs', 'default.yaml'), 
    help = 'path to the configuration file', 
    type = str
)
args = parser.parse_args();
cfg_filename = args.cfg

with open(cfg_filename, 'r') as cfg_file:
    cfg_params = yaml.load(cfg_file, Loader = yaml.FullLoader)

builder = ConfigBuilder(**cfg_params)

logger.info('Building models ...')

model = builder.get_model()

if builder.multigpu():
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    model.to(device)
else:
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    if device == torch.device('cpu'):
        raise EnvironmentError('No GPUs, cannot initialize multigpu training.')
    model.to(device)

logger.info('Building dataloaders ...')
train_dataloader = builder.get_dataloader(split = 'train')
test_dataloader = builder.get_dataloader(split = 'test')

logger.info('Checking checkpoints ...')
start_epoch = 0
max_epoch = builder.get_max_epoch()
stats_dir = builder.get_stats_dir()
checkpoint_file = os.path.join(stats_dir, 'checkpoint.tar')
if os.path.isfile(checkpoint_file):
    checkpoint = torch.load(checkpoint_file)
    model.load_state_dict(checkpoint['model_state_dict'])
    start_epoch = checkpoint['epoch']
    checkpoint_metrics = checkpoint['metrics']
    checkpoint_loss = checkpoint['loss']
    logger.info("Checkpoint {} (epoch {}) loaded.".format(checkpoint_file, start_epoch))

logger.info('Building optimizer and learning rate schedulers ...')
resume = (start_epoch > 0)
optimizer = builder.get_optimizer(model, resume = resume, resume_lr = builder.get_resume_lr())
lr_scheduler = builder.get_lr_scheduler(optimizer, resume = resume, resume_epoch = (start_epoch - 1 if resume else None))

if builder.multigpu():
    model = nn.DataParallel(model)

criterion = builder.get_criterion()
metrics = builder.get_metrics()


def train_one_epoch(epoch):
    logger.info('Start training process in epoch {}.'.format(epoch + 1))
    if lr_scheduler is not None:
        logger.info('Learning rate: {}.'.format(lr_scheduler.get_last_lr()))
    model.train()
    losses = []
    with tqdm(train_dataloader) as pbar:
        for data_dict in pbar:
            optimizer.zero_grad()
            data_dict = to_device(data_dict, device)
            res = model(data_dict['rgb'], data_dict['depth']) 
            depth_scale = data_dict['depth_max'] - data_dict['depth_min']
            res = res * depth_scale.reshape(-1, 1, 1) + data_dict['depth_min'].reshape(-1, 1, 1)
            data_dict['pred'] = res
            loss_dict = criterion(data_dict)
            loss = loss_dict['loss']
            loss.backward()
            optimizer.step()
            if 'smooth' in loss_dict.keys():
                pbar.set_description('Epoch {}, loss: {:.8f}, smooth loss: {:.8f}'.format(epoch + 1, loss.item(), loss_dict['smooth'].item()))
            else:
                pbar.set_description('Epoch {}, loss: {:.8f}'.format(epoch + 1, loss.item()))
            losses.append(loss.mean().item())
    mean_loss = np.stack(losses).mean()
    logger.info('Finish training process in epoch {}, mean training loss: {:.8f}'.format(epoch + 1, mean_loss))


def test_one_epoch(epoch):
    logger.info('Start testing process in epoch {}.'.format(epoch + 1))
    model.eval()
    metrics.clear()
    running_time = []
    losses = []
    with tqdm(test_dataloader) as pbar:
        for data_dict in pbar:
            data_dict = to_device(data_dict, device)
            with torch.no_grad():
                time_start = perf_counter()
                res = model(data_dict['rgb'], data_dict['depth'])
                time_end = perf_counter()
                depth_scale = data_dict['depth_max'] - data_dict['depth_min']
                res = res * depth_scale.reshape(-1, 1, 1) + data_dict['depth_min'].reshape(-1, 1, 1)
                data_dict['pred'] = res
                loss_dict = criterion(data_dict)
                loss = loss_dict['loss']
                _ = metrics.evaluate_batch(data_dict, record = True)
            duration = time_end - time_start
            if 'smooth' in loss_dict.keys():
                pbar.set_description('Epoch {}, loss: {:.8f}, smooth loss: {:.8f}'.format(epoch + 1, loss.item(), loss_dict['smooth'].item()))
            else:
                pbar.set_description('Epoch {}, loss: {:.8f}'.format(epoch + 1, loss.item()))
            losses.append(loss.item())
            running_time.append(duration)
    mean_loss = np.stack(losses).mean()
    avg_running_time = np.stack(running_time).mean()
    logger.info('Finish testing process in epoch {}, mean testing loss: {:.8f}, average running time: {:.4f}s'.format(epoch + 1, mean_loss, avg_running_time))
    metrics_result = metrics.get_results()
    metrics.display_results()
    return mean_loss, metrics_result


def train(start_epoch):
    if start_epoch != 0:
        min_loss = checkpoint_loss
        min_loss_epoch = start_epoch
        display_results(checkpoint_metrics, logger)
    else:
        min_loss = LOSS_INF
        min_loss_epoch = None
    for epoch in range(start_epoch, max_epoch):
        logger.info('--> Epoch {}/{}'.format(epoch + 1, max_epoch))
        train_one_epoch(epoch)
        loss, metrics_result = test_one_epoch(epoch)
        if lr_scheduler is not None:
            lr_scheduler.step()
        criterion.step()
        save_dict = {
            'epoch': epoch + 1,
            'model_state_dict': model.module.state_dict() if builder.multigpu() else model.state_dict(),
            'loss': loss,
            'metrics': metrics_result
        }
        torch.save(save_dict, os.path.join(stats_dir, 'checkpoint-epoch{}.tar'.format(epoch)))
        if loss < min_loss:
            min_loss = loss
            min_loss_epoch = epoch + 1
            torch.save(save_dict, os.path.join(stats_dir, 'checkpoint.tar'.format(epoch)))
    logger.info('Training Finished. Min testing loss: {:.6f}, in epoch {}'.format(min_loss, min_loss_epoch))


if __name__ == '__main__':
    train(start_epoch = start_epoch)
