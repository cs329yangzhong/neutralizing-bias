import sys

import json
import data
import models
import utils
import numpy as np
import logging
import argparse
import os
import time
import numpy as np
import glob

import torch
import torch.nn as nn
import torch.optim as optim
from tensorboardX import SummaryWriter

import evaluation
from cuda import CUDA



parser = argparse.ArgumentParser()
parser.add_argument(
    "--config",
    help="path to json config",
    required=True
)
parser.add_argument(
    "--bleu",
    help="do BLEU eval",
    action='store_true'
)
parser.add_argument(
    "--overfit",
    help="train continuously on one batch of data",
    action='store_true'
)
args = parser.parse_args()
config = json.load(open(args.config, 'r'))

working_dir = config['data']['working_dir']

if not os.path.exists(working_dir):
    os.makedirs(working_dir)

config_path = os.path.join(working_dir, 'config.json')
if not os.path.exists(config_path):
    with open(config_path, 'w') as f:
        json.dump(config, f)

# set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='%s/train_log' % working_dir,
)

console = logging.StreamHandler()
console.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console.setFormatter(formatter)
logging.getLogger('').addHandler(console)

logging.info('Reading data ...')
src, tgt = data.read_nmt_data(
    src=config['data']['src'],
    config=config,
    tgt=config['data']['tgt'],
    attribute_vocab=config['data']['attribute_vocab']
)

src_test, tgt_test = data.read_nmt_data(
    src=config['data']['src_test'],
    config=config,
    tgt=config['data']['tgt_test'],
    attribute_vocab=config['data']['attribute_vocab']
)
logging.info('...done!')


batch_size = config['data']['batch_size']
max_length = config['data']['max_len']
src_vocab_size = len(src['tok2id'])
tgt_vocab_size = len(tgt['tok2id'])


weight_mask = torch.ones(tgt_vocab_size)
weight_mask[tgt['tok2id']['<pad>']] = 0
loss_criterion = nn.CrossEntropyLoss(weight=weight_mask)
if CUDA:
    weight_mask = weight_mask.cuda()
    loss_criterion = loss_criterion.cuda()

torch.manual_seed(config['training']['random_seed'])
np.random.seed(config['training']['random_seed'])

model = models.StyleTransfer(
    src_vocab_size=src_vocab_size,
    tgt_vocab_size=tgt_vocab_size,
    pad_id_src=src['tok2id']['<pad>'],
    pad_id_tgt=tgt['tok2id']['<pad>'],
    config=config
)

logging.info('MODEL HAS %s params' %  model.count_params())
model, start_epoch = models.attempt_load_model(
    model=model,
    checkpoint_dir=working_dir)
if CUDA:
    model = model.cuda()

writer = SummaryWriter(working_dir)


if config['training']['optimizer'] == 'adam':
    lr = config['training']['learning_rate']
    optimizer = optim.Adam(model.parameters(), lr=lr)
elif config['training']['optimizer'] == 'sgd':
    lr = config['training']['learning_rate']
    optimizer = optim.SGD(model.parameters(), lr=lr)
else:
    raise NotImplementedError("Learning method not recommend for task")

epoch_loss = []
start_since_last_report = time.time()
words_since_last_report = 0
losses_since_last_report = []
best_metric = 0.0
best_epoch = 0
cur_metric = 0.0 # log perplexity or BLEU
num_batches = len(src['content']) / batch_size
with open(working_dir + '/stats_labels.csv', 'w') as f:
    f.write(utils.config_key_string(config) + ',%s,%s\n' % (
        ('bleu' if args.bleu else 'dev_loss'), 'best_epoch'))

# TODO -- reinable softmax scheduling
softmax_temp = 1#config['model']['self_attn_temp']
STEP = 0
for epoch in range(start_epoch, config['training']['epochs']):
    # if epoch > 3 and cur_metric == 0 or epoch > 7 and cur_metric < 10 or epoch > 15 and cur_metric < 15:
    #     logging.info('QUITTING...NOT LEARNING WELL')
    #     with open(working_dir + '/stats.csv', 'w') as f:
    #         f.write(utils.config_val_string(config) + ',%s,%s\n' % (
    #             best_metric, best_epoch))
    #     break

    if cur_metric > best_metric:
        # rm old checkpoint
        for ckpt_path in glob.glob(working_dir + '/model.*'):
            os.system("rm %s" % ckpt_path)
        # replace with new checkpoint
        torch.save(model.state_dict(), working_dir + '/model.%s.ckpt' % epoch)

        best_metric = cur_metric
        best_epoch = epoch - 1

    losses = []
    for i in range(0, len(src['content']), batch_size):
        if args.overfit:
            i = 50

        batch_idx = i / batch_size

        input_content, input_aux, output = data.minibatch(
            src, tgt, i, batch_size, max_length, config['model']['model_type'])
        input_lines_src, _, srclens, srcmask, _ = input_content
        input_ids_aux, _, auxlens, auxmask, _ = input_aux
        input_lines_tgt, output_lines_tgt, _, _, _ = output
        
        
#        TODO FROM HERE!!!!!!!!
        decoder_logit, decoder_probs = model(
            input_lines_src, input_lines_tgt, srcmask, srclens,
            input_ids_aux, auxlens, auxmask)

        optimizer.zero_grad()

        loss = loss_criterion(
            decoder_logit.contiguous().view(-1, tgt_vocab_size),
            output_lines_tgt.view(-1)
        )
        losses.append(loss.data[0])
        losses_since_last_report.append(loss.data[0])
        epoch_loss.append(loss.data[0])
        loss.backward()
        norm = nn.utils.clip_grad_norm_(model.parameters(), config['training']['max_norm'])

        writer.add_scalar('stats/grad_norm', norm, STEP)

        optimizer.step()

        if args.overfit or batch_idx % config['training']['batches_per_report'] == 0:

            s = float(time.time() - start_since_last_report)
            wps = (batch_size * config['training']['batches_per_report']) / s
            avg_loss = np.mean(losses_since_last_report)
            info = (epoch, batch_idx, num_batches, wps, avg_loss, cur_metric)
            writer.add_scalar('stats/WPS', wps, STEP)
            writer.add_scalar('stats/loss', avg_loss, STEP)
            logging.info('EPOCH: %s ITER: %s/%s WPS: %.2f LOSS: %.4f METRIC: %.4f' % info)
            start_since_last_report = time.time()
            words_since_last_report = 0
            losses_since_last_report = []

        if not args.overfit and batch_idx % config['training']['batches_per_sampling'] == 0:
            logging.info('PRINTING SAMPLE...')

            model.eval()
            tgt_pred = evaluation.decode_minibatch(
                config['data']['max_len'], tgt['tok2id']['<s>'], 
                model=model, 
                src_input=input_lines_src[:3],
                srclens=srclens[:3],
                srcmask=srcmask[:3],
                temp=softmax_temp)
            model.train()

            tgt_pred = tgt_pred.data.cpu().numpy()
            tgt_gold = output_lines_tgt.data.cpu().numpy()[:3]

            for s_pred, s_gold in zip(tgt_pred, tgt_gold):
                pred_line = [tgt['id2tok'][x] for x in s_pred]
                if '</s>' in pred_line:
                    pred_line = ' '.join(pred_line[:pred_line.index('</s>')])
                else:
                    pred_line = ' '.join(pred_line)
                gold_line = [tgt['id2tok'][x] for x in s_gold]
                try:
                    gold_line = ' '.join(gold_line[:gold_line.index('</s>')])
                except:
                    gold_line = ' '.join(gold_line)
                logging.info('PRED: %s' % pred_line)
                logging.info('GOLD: %s' % gold_line)
                logging.info('')

        STEP += 1
    if args.overfit:
        continue

    logging.info('EPOCH %s COMPLETE. EVALUATING...' % epoch)
    start = time.time()
    model.eval()
    dev_loss = evaluation.evaluate_lpp(
            model, src_test, tgt_test, config, softmax_temp)

    writer.add_scalar('eval/loss', dev_loss, epoch)

    if args.bleu and epoch >= config['training'].get('bleu_start_epoch', 1):
        cur_metric, preds, golds = evaluation.evaluate_bleu(
            model, src_test, tgt_test, config, softmax_temp)
        with open(working_dir + '/preds.%s' % epoch, 'w') as f:
            f.write('\n'.join(preds) + '\n')
        with open(working_dir + '/golds.%s' % epoch, 'w') as f:
            f.write('\n'.join(golds) + '\n')

        writer.add_scalar('eval/bleu', cur_metric, epoch)

    else:
        cur_metric = dev_loss

    model.train()

    logging.info('METRIC: %s. TIME: %.2fs CHECKPOINTING...' % (
        cur_metric, (time.time() - start)))
    avg_loss = np.mean(epoch_loss)
    epoch_loss = []

writer.close()
with open(working_dir + '/stats.csv', 'w') as f:
    f.write(utils.config_val_string(config) + ',%s,%s\n' % (
        best_metric, best_epoch))
