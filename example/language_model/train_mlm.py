import warnings
warnings.filterwarnings("ignore")
import sys
# colab: /content/drive/My Drive/Colab Notebooks/transformer
# local: ../
sys.path.append('../')

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, random_split

from tqdm import tqdm

from transformers import BertTokenizer
from transformers.optimization import AdamW

import os
import json
import logging
from datetime import datetime
from example.language_model.dataset import DatasetForMLM
from example.language_model.arg import ModelConfig
from example.language_model.schedule import WarmupLinearSchedule
from model.transformer import TransformerLM

# from torchsummary import summary


class TransformeLMTrainer(object):
    def __init__(self,
                 dataset,
                 model,
                 tokenizer,
                 max_len,
                 model_name,
                 checkpoint_path,
                 device=None,
                 train_batch_size=8,
                 eval_batch_size=None,
                 tb_writer=False,
                 tb_dir='./tb_logs',
                 log_dir='../logs'):
        self.dataset = dataset
        self.model = model
        self.tokenizer = tokenizer
        self.max_len = max_len
        self.model_name = model_name
        self.checkpoint_path = checkpoint_path
        self.device = device
        self.n_gpu = torch.cuda.device_count() if torch.cuda.is_available() else 0
        self.train_batch_size = train_batch_size
        self.eval_batch_size = eval_batch_size
        self.tb_writer = tb_writer
        self.log_dir = log_dir

        if device is None:
            self.device = 'cuda:0' if torch.cuda.is_available() else 'cpu'

        if eval_batch_size is None:
            self.eval_batch_size = train_batch_size

        if tb_writer:
            from torch.utils.tensorboard import SummaryWriter
            self.writer = SummaryWriter(log_dir=tb_dir)

        logging.basicConfig(filename=f'{log_dir}/{self.model_name}-{datetime.now().date()}.log', level=logging.INFO)

    def build_dataloaders(self, train_test_split=0.1, train_shuffle=True, eval_shuffle=True):
        dataset_len = len(self.dataset)
        eval_len = int(dataset_len * train_test_split)
        train_len = dataset_len - eval_len
        train_dataset, eval_dataset = random_split(self.dataset, (train_len, eval_len))
        train_loader = DataLoader(train_dataset, batch_size=self.train_batch_size, shuffle=train_shuffle)
        eval_loader = DataLoader(eval_dataset, batch_size=self.eval_batch_size, shuffle=eval_shuffle)
        logging.info(f'''train_dataloader size: {len(train_loader.dataset)} | shuffle: {train_shuffle} | eval_dataloader size: {len(eval_loader.dataset)} | shuffle: {eval_shuffle}''')
        return train_loader, eval_loader

    def train(self,
              epochs,
              train_dataloader,
              eval_dataloader,
              optimizer,
              scheduler,
              log_steps,
              ckpt_steps):

        loss_fn = nn.CrossEntropyLoss()
        losses = {}
        global_steps = 0
        local_steps = 0
        step_loss = 0.0
        start_epoch = 0
        start_step = 0

        # Load Checkpoint
        if os.path.isfile(f'{self.checkpoint_path}/{self.model_name}.pth'):
            checkpoint = torch.load(f'{self.checkpoint_path}/{self.model_name}.pth', map_location=self.device)
            start_epoch = checkpoint['epoch']
            losses = checkpoint['losses']
            global_steps = checkpoint['train_step']
            start_step = global_steps if start_epoch==0 else global_steps % len(train_dataloader)

            self.model.load_state_dict(checkpoint['model_state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer_state_dict'])

        # Model Setting
        self.model.train()
        self.model.to(self.device)

        logging.info(f'{datetime.now()} | Moved model to: {self.device} | train_batch_size: {self.train_batch_size} | eval_batch_size: {self.eval_batch_size}')
        logging.info(f'{datetime.now()} | Epochs: {epochs} | log_steps: {log_steps} | ckpt_steps: {ckpt_steps}')

        for epoch in range(start_epoch, epochs): #tqdm(range(epochs), desc='Epochs', position=0):
            logging.info(f'{datetime.now()} | Epoch: {epoch}')
            pb = tqdm(enumerate(train_dataloader),
                      desc=f'Epoch-{epoch} Iterator',
                      total=len(train_dataloader),
                      bar_format='{l_bar}{bar:10}{r_bar}'
                      )
            for step, batch in pb:
                if step < start_step:
                    continue
                inputs, input_mask, labels = batch # _ is input_mask
                inputs, input_mask, labels = inputs.to(self.device), input_mask.to(self.device), labels.to(self.device)
                output,_ = self.model(inputs, input_mask)

                # only calculating loss on masked tokens
                loss_mx = labels != -100
                output= output[loss_mx].view(-1, self.tokenizer.vocab_size)
                labels = labels[loss_mx].view(-1)

                loss = loss_fn(output, labels)
                loss.backward()

                step_loss += loss.item()
                losses[global_steps] = loss.item()
                local_steps += 1
                global_steps += 1

                scheduler.step()
                optimizer.step()
                optimizer.zero_grad()

                # print log
                if global_steps % log_steps == 0:
                    if self.tb_writer:
                        self.writer.add_scalar('Train/Loss', step_loss / local_steps, global_steps)
                        self.writer.close()
                    pb.set_postfix_str(f'''{datetime.now()} | Train Loss: {step_loss / local_steps} | Learning Rate: {scheduler.get_lr()} | Steps: {global_steps}''')
                    step_loss = 0.0
                    local_steps = 0
                # save model & log
                if global_steps % ckpt_steps == 0:
                    self.save(epoch, self.model, optimizer, losses, global_steps)
                    logging.info(f'{datetime.now()} | Saved checkpoint to: {self.checkpoint_path}')
                    with open(f'{self.log_dir}/{self.model_name}-train-log.json', 'w') as results_file:
                      json.dump(losses, results_file)
                      results_file.close()

            # evaluate
            self.evaluate(eval_dataloader)
            self.model.train()
            start_step = 0
        # save model
        self.save(epochs,self.model,optimizer,losses, global_steps)

    def evaluate(self, dataloader):
        loss_fn = nn.CrossEntropyLoss()

        if self.n_gpu > 1 and not isinstance(self.model, nn.DataParallel):
            self.model = nn.DataParallel(self.model)

        self.model.eval()

        eval_loss = 0.0
        eval_steps = 0

        logging.info(f'{datetime.now()} | Evaluating...')
        for step, batch in tqdm(enumerate(dataloader),
                                desc='Evaluating',
                                leave=True,
                                total=len(dataloader),
                                bar_format='{l_bar}{bar:10}{r_bar}'):
            inputs, input_mask, labels = batch
            inputs, input_mask, labels = inputs.to(self.device), input_mask.to(self.device), labels.to(self.device)

            with torch.no_grad():
                output,_ = self.model(inputs, input_mask)

            loss_mx = labels != -100
            output_ids = output[loss_mx].view(-1, self.tokenizer.vocab_size)
            labels = labels[loss_mx].view(-1)
            tmp_eval_loss = loss_fn(output_ids, labels)

            if self.n_gpu > 1:
                tmp_eval_loss = tmp_eval_loss.mean()

            eval_loss += tmp_eval_loss.item()
            eval_steps += 1

            total_eval_loss = eval_loss/eval_steps

            if self.tb_writer:
                self.writer.add_scalar('Eval/Loss', eval_loss, eval_steps)
                self.writer.close()
            logging.info(f'{datetime.now()} | Step: {step} | Eval Loss: {total_eval_loss}')
            with open(f'{self.log_dir}/{self.model_name}_eval_results.txt', 'a+') as results_file:
                results_file.write(f'{datetime.now()} | Step: {step} | Eval Loss: {total_eval_loss}\n')
                results_file.close()

    def save(self, epoch, model, optimizer, losses, train_step):
        torch.save({
            'epoch': epoch,  # 현재 학습 epoch
            'model_state_dict': model.state_dict(),  # 모델 저장
            'optimizer_state_dict': optimizer.state_dict(),  # 옵티마이저 저장
            'losses': losses,  # Loss 저장
            'train_step': train_step,  # 현재 진행한 학습
        }, f'{self.checkpoint_path}/{self.model_name}.pth')

def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)

def main():
    torch.manual_seed(9)
    # base_path = '/content/drive/My Drive/Colab Notebooks/transformer'
    base_path = '../..'
    log_dir = f'{base_path}/log'
    # config_path = f'{}'

    # Config
    config = ModelConfig(config_path='./config.json').get_config()

    # Tokenizer
    tokenizer = BertTokenizer(vocab_file=config.vocab_path, do_lower_case=False)

    dataset = DatasetForMLM(tokenizer, config.max_seq_len, path=config.data_path)

    # Model
    model = TransformerLM(
        vocab_size=tokenizer.vocab_size,
        dim=config.dim,
        depth=config.depth,
        max_seq_len=config.max_seq_len,
        head_num=config.n_head
    )

    # print(count_parameters(model))
    logging.info(f'{datetime.now()} | Number of parameter: {"{:,}".format(count_parameters(model))}')

    # model summary
    # summary(model,(config.batch_size, config.max_seq_len), batch_size=config.batch_size)

    # traniner
    trainer = TransformeLMTrainer(dataset, model, tokenizer,
                                  model_name=config.model_name,
                                  checkpoint_path=config.checkpoint_path,
                                  max_len=config.max_seq_len,
                                  train_batch_size=config.batch_size,
                                  eval_batch_size=config.batch_size,
                                  log_dir=log_dir)
    # dataloader
    train_dataloader, eval_dataloader = trainer.build_dataloaders(train_test_split=0.1)

    # Prepare optimizer
    param_optimizer = list(model.named_parameters())
    no_decay = ['bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
      {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)],
       'weight_decay': 0.01},
      {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)],
       'weight_decay': 0.0}
    ]

    learning_rate = 3e-5
    adam_epsilon = 1e-6

    # warmup_proportion = 0.1
    # num_train_epochs = 10.0
    # max_grad_norm = 1.0
    # adam_epsilon = 1e-6
    # weight_decay = 0.01
    # num_train_optimization_steps = int(len(train_dataloader) / config.batch_size) * num_train_epochs

    optimizer = AdamW(optimizer_grouped_parameters,
                      lr=learning_rate,
                      eps=adam_epsilon)
    # scheduler = WarmupLinearSchedule(optimizer,
    #                                  warmup_steps=num_train_optimization_steps * 0.1,
    #                                  t_total=num_train_optimization_steps)
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer = optimizer,
        max_lr = 0.0001,
        pct_start = 0.01,
        anneal_strategy = "linear",
        epochs = config.epochs,
        steps_per_epoch = len(train_dataloader)
    )
    trainer.train(epochs=config.epochs,
                  train_dataloader=train_dataloader,
                  eval_dataloader=eval_dataloader,
                  optimizer=optimizer,
                  scheduler=scheduler,
                  log_steps=config.log_steps,
                  ckpt_steps=config.ckpt_steps)

if __name__ == '__main__':
    main()
