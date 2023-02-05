import os.path
import math
import argparse
import time
import random
import numpy as np
from collections import OrderedDict
import logging
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
import torch

from models.network_swinir import SwinIR as swinIR
from utils import utils_logger
from utils import utils_image as util
from utils import utils_option as option
from utils.utils_dist import get_dist_info, init_dist

from data.select_dataset import define_Dataset
from models.select_model import define_Model


'''
# --------------------------------------------
# training code for MSRResNet
# --------------------------------------------
# Kai Zhang (cskaizhang@gmail.com)
# github: https://github.com/cszn/KAIR
# --------------------------------------------
# https://github.com/xinntao/BasicSR
# --------------------------------------------
'''


def main(json_path='options/train_msrresnet_psnr.json'):

    '''
    # ----------------------------------------
    # Step--1 (prepare opt)
    # ----------------------------------------
    '''

    parser = argparse.ArgumentParser()
    parser.add_argument('--opt', type=str, default=json_path, help='Path to option JSON file.')
    parser.add_argument('--launcher', default='pytorch', help='job launcher')
    parser.add_argument('--local_rank', type=int, default=0)
    parser.add_argument('--dist', default=False)

    opt = option.parse(parser.parse_args().opt, is_train=True)
    opt['dist'] = parser.parse_args().dist

    # ----------------------------------------
    # distributed settings
    # ----------------------------------------
    if opt['dist']:
        init_dist('pytorch')
    opt['rank'], opt['world_size'] = get_dist_info()

    if opt['rank'] == 0:
        util.mkdirs((path for key, path in opt['path'].items() if 'pretrained' not in key))

    # ----------------------------------------
    # update opt
    # ----------------------------------------
    # -->-->-->-->-->-->-->-->-->-->-->-->-->-
    init_iter_G, init_path_G = option.find_last_checkpoint(opt['path']['models'], net_type='G')
    init_iter_E, init_path_E = option.find_last_checkpoint(opt['path']['models'], net_type='E')
    opt['path']['pretrained_netG'] = init_path_G
    opt['path']['pretrained_netE'] = init_path_E
    init_iter_optimizerG, init_path_optimizerG = option.find_last_checkpoint(opt['path']['models'], net_type='optimizerG')
    opt['path']['pretrained_optimizerG'] = init_path_optimizerG
    current_step = max(init_iter_G, init_iter_E, init_iter_optimizerG)

    border = opt['scale']
    # --<--<--<--<--<--<--<--<--<--<--<--<--<-

    # ----------------------------------------
    # save opt to  a '../option.json' file
    # ----------------------------------------
    if opt['rank'] == 0:
        option.save(opt)

    # ----------------------------------------
    # return None for missing key
    # ----------------------------------------
    opt = option.dict_to_nonedict(opt)

    # ----------------------------------------
    # configure logger
    # ----------------------------------------
    if opt['rank'] == 0:
        logger_name = 'train'
        utils_logger.logger_info(logger_name, os.path.join(opt['path']['log'], logger_name+'.log'))
        logger = logging.getLogger(logger_name)
        logger.info(option.dict2str(opt))

    # ----------------------------------------
    # seed
    # ----------------------------------------
    #seed = opt['train']['manual_seed']
    seed = 7142
    if seed is None:
        seed = random.randint(1, 10000)
    print('Random seed: {}'.format(seed))
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

    '''
    # ----------------------------------------
    # Step--2 (creat dataloader)
    # ----------------------------------------
    '''

    # ----------------------------------------
    # 1) create_dataset
    # 2) creat_dataloader for train and test
    # ----------------------------------------
    for phase, dataset_opt in opt['datasets'].items():
        if phase == 'train':
            train_set = define_Dataset(dataset_opt)
            train_size = int(math.ceil(len(train_set) / dataset_opt['dataloader_batch_size']))
            if opt['rank'] == 0:
                logger.info('Number of train images: {:,d}, iters: {:,d}'.format(len(train_set), train_size))
            if opt['dist']:
                train_sampler = DistributedSampler(train_set, shuffle=dataset_opt['dataloader_shuffle'], drop_last=True, seed=seed)
                train_loader = DataLoader(train_set,
                                          batch_size=dataset_opt['dataloader_batch_size']//opt['num_gpu'],
                                          shuffle=False,
                                          num_workers=dataset_opt['dataloader_num_workers']//opt['num_gpu'],
                                          drop_last=True,
                                          pin_memory=True,
                                          sampler=train_sampler)
            else:
                train_loader = DataLoader(train_set,
                                          batch_size=dataset_opt['dataloader_batch_size'],
                                          shuffle=dataset_opt['dataloader_shuffle'],
                                          num_workers=dataset_opt['dataloader_num_workers'],
                                          drop_last=True,
                                          pin_memory=True)

        elif phase == 'test':
            test_set = define_Dataset(dataset_opt)
            test_loader = DataLoader(test_set, batch_size=1,
                                     shuffle=False, num_workers=1,
                                     drop_last=False, pin_memory=True)
            
        else:
            raise NotImplementedError("Phase [%s] is not recognized." % phase)

    
    '''
    # ----------------------------------------
    # Teacher Model 추가 시작
    # ----------------------------------------
    ''' 
    teacher_model = swinIR(upscale=2, in_chans=3, img_size=64, window_size=8,
                    img_range=1., depths=[6, 6, 6, 6], embed_dim=60, num_heads=[6, 6, 6, 6],
                    mlp_ratio=2, upsampler='pixelshuffledirect', resi_connection='1conv')
    teacher_model.eval()
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    teacher_model = teacher_model.to(device)
    pretrained_model = torch.load("./model_zoo/002_lightweightSR_DIV2K_s64w8_SwinIR-S_x2.pth")
    teacher_model.load_state_dict(pretrained_model['params'] if 'params' in pretrained_model.keys() else pretrained_model, strict=True)
   

    '''
    # ----------------------------------------
    # Step--3 (initialize model)
    # ----------------------------------------
    '''

    model = define_Model(opt)
    model.init_train()
    model.define_teacher(teacher_model)
    #model.netG.module.set_block_num(3)
    if opt['rank'] == 0:
        logger.info(model.info_network())
        logger.info(model.info_params())


    '''
    # ----------------------------------------
    # Teacher Model의 가중치를 Student Model에 적용시키기
    # ----------------------------------------
    ''' 
    # from collections import OrderedDict

    # init_layer_name_simple = ['conv_first', 'upsample']
    # init_layer_name = []

    # for k, v in pretrained_model['params'].items():
    #     if any(x in k for x in init_layer_name_simple):
    #         init_layer_name.append(k)
        
    # model.netG.load_state_dict(
    #     OrderedDict(('module.' + k, pretrained_model['params'][k]) for k in init_layer_name),
    #     strict=False
    # )

    test_datas_opt = [{'name': 'test_dataset', 'dataset_type': 'sr', 'dataroot_H': './testsets/HR/set14', 'dataroot_L': './testsets/LR/set14', 'phase': 'test', 'scale': 2, 'n_channels': 3, 'H_size' : 128},
    {'name': 'test_dataset', 'dataset_type': 'sr', 'dataroot_H': './testsets/HR/set5', 'dataroot_L': './testsets/LR/set5', 'phase': 'test', 'scale': 2, 'n_channels': 3, 'H_size' : 128},
    {'name': 'test_dataset', 'dataset_type': 'sr', 'dataroot_H': './testsets/HR/Manga109', 'dataroot_L': './testsets/LR/Manga109', 'phase': 'test', 'scale': 2, 'n_channels': 3, 'H_size' : 128},
    {'name': 'test_dataset', 'dataset_type': 'sr', 'dataroot_H': './testsets/HR/Urban', 'dataroot_L': './testsets/LR/Urban', 'phase': 'test', 'scale': 2, 'n_channels': 3, 'H_size' : 128}
    ]

    f = open(f"./results/result.txt", "a")
    f.write(opt['path']['models']+"\n")

    for test_data_opt in test_datas_opt:
        avg_psnr = 0.0
        idx = 0
        print(test_data_opt)
        test_set = define_Dataset(test_data_opt)
        test_loader = DataLoader(test_set, batch_size=1,
                                    shuffle=False, num_workers=1,
                                    drop_last=False, pin_memory=True)

        for test_data in test_loader:
            idx += 1
            image_name_ext = os.path.basename(test_data['L_path'][0])
            img_name, ext = os.path.splitext(image_name_ext)

            img_dir = os.path.join(opt['path']['images'], img_name)
            util.mkdir(img_dir)

            model.feed_data(test_data)
            model.test()

            visuals = model.current_visuals()
            E_img = util.tensor2uint(visuals['E'])
            H_img = util.tensor2uint(visuals['H'])

            # -----------------------
            # save estimated image E
            # -----------------------
            save_img_path = os.path.join(img_dir, '{:s}_{:d}.png'.format(img_name, current_step))
            util.imsave(E_img, save_img_path)

            # -----------------------
            # calculate PSNR
            # -----------------------
            current_psnr = util.calculate_psnr(E_img, H_img, border=border)

            logger.info('{:->4d}--> {:>10s} | {:<4.2f}dB'.format(idx, image_name_ext, current_psnr))

            avg_psnr += current_psnr

        avg_psnr = avg_psnr / idx

        # testing log
        logger.info('Average PSNR : {:<.2f}dB\n'.format(avg_psnr))
        f.write("\t" + str(test_data_opt['dataroot_H']) +' '+ str(avg_psnr)+"\n")

    f.write("\n")
    f.close()


if __name__ == '__main__':
    main()