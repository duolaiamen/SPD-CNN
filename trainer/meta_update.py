# +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
## Modified from: https://github.com/yaoyao-liu/meta-transfer-learning
## This source code is licensed under the MIT-style license found in the
## LICENSE file in the root directory of this source tree
##+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
""" Trainer for meta-train phase. """
import os.path as osp
import os
import tqdm
import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader
from dataloader.TaskSampler import TaskTrainingSampler
from dataloader.samplers import CategoriesSampler
from models.mtl import MtlLearner
from sklearn.metrics import roc_auc_score, precision_score, recall_score, accuracy_score, f1_score
from sklearn.preprocessing import LabelBinarizer
from utils.misc import Averager, Timer, count_acc, compute_confidence_interval, ensure_path
from tensorboardX import SummaryWriter
import time


class MetaTrainer(object):
    """The class that contains the code for the meta-train phase and meta-eval phase."""

    def __init__(self, args):
        # Set the folder to save the records and checkpoints
        log_base_dir = './logs/'
        if not osp.exists(log_base_dir):
            os.mkdir(log_base_dir)
        meta_base_dir = osp.join(log_base_dir, 'meta')
        if not osp.exists(meta_base_dir):
            os.mkdir(meta_base_dir)
        save_path1 = '_'.join([args.dataset, args.model_type, 'MTL'])
        save_path2 = 'shot' + str(args.shot) + '_way' + str(args.way) + '_query' + str(args.train_query) + '_lr1' + str(
            args.meta_lr1) + '_lr2' + str(args.meta_lr2) + \
                     '_batch' + str(args.num_batch) + '_maxepoch' + str(args.max_epoch) + \
                     '_baselr' + str(args.base_lr) + '_updatestep' + str(args.update_step) + '_stepsize' + str(
            args.step_size) + 'cls_lay-' + str(args.num_cls_lay) + '-' + str(args.num_cls_hidden)
        args.save_path = meta_base_dir + '/' + save_path1 + '_' + save_path2 + '_' + str(args.meta_label)
        ensure_path(args.save_path)

        # Set args to be shareable in the class
        self.args = args

        # Load meta-train set
        if args.dataset == 'BNCI2015004':
            from dataloader.DataSetLoader_BNCI2015004 import DataSetLoader_BNCI2015004 as Dataset
        elif args.dataset == 'BNCI2014001':
            from dataloader.DataSetLoader_BNCI2014001 import DataSetLoader_BNCI2014001 as Dataset
        elif args.dataset == 'Schirrmeister2017':
            from dataloader.DataSetLoader_Schirrmeister2017 import DataSetLoader_Schirrmeister2017 as Dataset
        elif args.dataset == 'BNCI2014001_SPD':
            from dataloader.DataSetLoader_BNCI2014001_SPD import DataSetLoader_BNCI2014001_SPD as Dataset
        elif args.dataset == 'Schirrmeister2017_SPD':
            from dataloader.DataSetLoader_Schirrmeister2017_SPD import DataSetLoader_Schirrmeister2017_SPD as Dataset
        elif args.dataset == 'BNCI2015004_SPD':
            from dataloader.DataSetLoader_BNCI2015004_SPD import DataSetLoader_BNCI2015004_SPD as Dataset
        else:
            assert print('wrong dataset input')
        print("Preparing dataset loader")

        self.trainset = Dataset('train', self.args, train_aug=False, TrainSubjects=self.args.TrainSubjects,
                                ValSubject=self.args.ValSubject, TestSubject=self.args.TestSubject,
                                BinaryClassify=args.BinaryClassify)
        self.train_sampler = TaskTrainingSampler(self.trainset.label, self.args.num_batch, self.args.way,
                                                 self.args.shot + self.args.train_query, self.trainset.sub_div)
        self.train_loader = DataLoader(dataset=self.trainset, batch_sampler=self.train_sampler, num_workers=8,pin_memory=True)

        # Load meta-val set
        self.valset = Dataset('val', self.args, TrainSubjects=self.args.TrainSubjects, ValSubject=self.args.ValSubject,
                              TestSubject=self.args.TestSubject,
                              BinaryClassify=args.BinaryClassify)  # PS:import DataSetLoader_BNCI2015004 as Dataset
        self.val_sampler = CategoriesSampler(self.valset.label, 20, self.args.way,
                                             self.args.shot + self.args.val_query)  # 代表丢多少个batch进去验证然后求平均值
        self.val_loader = DataLoader(dataset=self.valset, batch_sampler=self.val_sampler, num_workers=8,
                                     pin_memory=True)

        # Set pretrain class number
        num_class_pretrain = self.trainset.num_class
        in_chans = self.trainset.in_chans
        input_time_length = self.trainset.time_step

        # Build meta-transfer learning model
        self.model = MtlLearner(self.args, mode='meta', num_cls=num_class_pretrain, in_chans=in_chans,
                                input_time_length=input_time_length)

        # Set optimizer
        self.optimizer = torch.optim.Adam(
            [{'params': filter(lambda p: p.requires_grad, self.model.encoder.parameters())}, \
             {'params': self.model.base_learner.parameters(), 'lr': self.args.meta_lr2}], lr=self.args.meta_lr1)
        # Set learning rate scheduler
        self.lr_scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, step_size=self.args.step_size,
                                                            gamma=self.args.gamma)

        # load pretrained model without classifier #
        self.model_dict = self.model.state_dict()
        if self.args.init_weights is not None:
            pretrained_dict = torch.load(self.args.init_weights)['params']
        else:
            # Set the folder to save the records and checkpoints
            log_base_dir = './logs/'
            if not osp.exists(log_base_dir):
                os.mkdir(log_base_dir)
            pre_base_dir = osp.join(log_base_dir, 'pre')
            if not osp.exists(pre_base_dir):
                os.mkdir(pre_base_dir)
            save_path1 = '_'.join([args.dataset, args.model_type])
            save_path2 = 'batchsize' + str(args.pre_batch_size) + '_lr' + str(args.pre_lr) + '_gamma' + str(
                args.pre_gamma) + '_step' + \
                         str(args.pre_step_size) + '_maxepoch' + str(args.pre_max_epoch)

            save_path3 = 'TrainSubjects'
            for subject in args.TrainSubjects:
                save_path3 += str(subject)
            save_path3 += '_TestSubject'
            for subject in args.TestSubject:
                save_path3 += str(subject)
            #
            if args.BinaryClassify == 1:
                save_path3 += '_Binary'
            pre_save_path = pre_base_dir + '/' + save_path1 + '_' + save_path2 + '_' + save_path3 + '_' + str(
                args.pre_train_label)  #
            # pretrained_dict = torch.load(osp.join(pre_save_path, 'max_acc.pth'))['params']  #
            pretrained_dict = torch.load(osp.join(pre_save_path, 'meta_val_max_acc.pth'))['params']  #

        pretrained_dict = {'encoder.' + k: v for k, v in
                           pretrained_dict.items()}  #
        pretrained_dict = {k: v for k, v in pretrained_dict.items() if
                           k in self.model_dict}  #
        self.model_dict.update(
            pretrained_dict)  #
        #
        self.model.load_state_dict(self.model_dict)
        # Set model to GPU
        if torch.cuda.is_available():
            torch.backends.cudnn.benchmark = True
            self.model = self.model.cuda()

    def save_model(self, name):
        """The function to save checkpoints.
        Args:
          name: the name for saved checkpoint
        """
        torch.save(dict(params=self.model.state_dict()), osp.join(self.args.save_path, name + '.pth'))

    def train(self):
        """The function for the meta-train phase."""
        def multiclass_roc_auc_score(y_test, y_pred, average="macro"):
            lb = LabelBinarizer()
            lb.fit(y_test)
            y_test = lb.transform(y_test)
            y_pred = lb.transform(y_pred)
            return roc_auc_score(y_test, y_pred, average=average)
        # Set the meta-train log
        trlog = {}
        trlog['args'] = vars(self.args)
        trlog['train_loss'] = []
        trlog['meta_val_loss'] = []
        trlog['train_acc'] = []
        trlog['meta_val_acc'] = []
        trlog['max_acc'] = 0.0
        trlog['max_acc_epoch'] = 0

        # Set the timer
        timer = Timer()
        # Set global count to zero
        global_count = 0
        # Set tensorboardX
        writer = SummaryWriter(comment=self.args.save_path)



        # Start meta-train
        print("--------------begin of meta train-------------")

        # Generate the labels for train set of the episodes
        label_shot = torch.arange(self.args.way).repeat(self.args.shot)
        if torch.cuda.is_available():
            label_shot = label_shot.type(torch.cuda.LongTensor)
        else:
            label_shot = label_shot.type(torch.LongTensor)
        # start_time = time.time()
        for epoch in range(1, self.args.max_epoch + 1):
            start_time = time.time()
            # Update learning rate #
            # self.lr_scheduler.step()
            # Set the model to train mode
            self.model.train()
            # Set averager classes to record training losses and accuracies
            train_loss_averager = Averager()
            train_acc_averager = Averager()
            # Using tqdm to read samples from train loader
            tqdm_gen = tqdm.tqdm(self.train_loader)
            # num_meta_batch=4
            num_meta_batch=self.args.meta_batch_size
            task_loss=[]
            task_acc=[]
            for i, batch in enumerate(tqdm_gen, 1):
                # Update global count number
                global_count = global_count + 1
                # gpu or cpu
                if torch.cuda.is_available():
                    data, _ = [_.cuda() for _ in batch]
                else:
                    data = batch[0]
                del batch
                p = self.args.shot * self.args.way
                data_shot, data_query = data[:p], data[p:]
                del data
                # Output logits for model
                logits = self.model((data_shot, label_shot, data_query))#innerloop update
                del data_shot, data_query
                torch.cuda.empty_cache()

                #------------inner val loop --------#
                # Generate the labels for test set of the episodes during meta-train updates
                label = torch.arange(self.args.way).repeat(self.args.train_query)
                if torch.cuda.is_available():
                    label = label.type(torch.cuda.LongTensor)
                else:
                    label = label.type(torch.LongTensor)
                # Calculate inner-loop val loss and  inner-loop val acc /auc
                loss = F.cross_entropy(logits, label)#innerloop-val loss
                acc = count_acc(logits, label)# innerloop-val acc

                #Collect loss and acc for outer loop or outdate outer loop
                if i%num_meta_batch==0:

                    task_loss.append(loss)
                    task_acc.append(acc)

                    # --Update outer loop--
                    # Loss backwards and optimizer updattes
                    self.optimizer.zero_grad()
                    meta_batch_loss = torch.stack(task_loss).mean()
                    meta_batch_loss.backward()  #
                    self.optimizer.step()
                    self.lr_scheduler.step()  #
                    task_acc = np.mean(task_acc)  #

                    writer.add_scalar('data/meta_train_loss', float(meta_batch_loss), global_count)
                    writer.add_scalar('data/meta_train_acc', float(task_acc), global_count)

                    # Add loss and accuracy for the averagers
                    train_loss_averager.add(meta_batch_loss.item())
                    train_acc_averager.add(task_acc)

                    task_loss =[]
                    task_acc = []
                else:
                    task_loss.append(loss)#meta-loss
                    task_acc.append(acc)
                del loss,acc
                torch.cuda.empty_cache()
            torch.cuda.empty_cache()
            # Update the averagers
            train_loss_averager = train_loss_averager.item()
            train_acc_averager = train_acc_averager.item()

            print("--- %s seconds ---" % (time.time() - start_time))
            # Start validation for this epoch, set model to eval mode
            self.model.eval()

            # Set averager classes to record validation losses and accuracies
            val_loss_averager = Averager()
            val_acc_averager = Averager()

            # Generate the labels for test set of the episodes during meta-val for this epoch
            label = torch.arange(self.args.way).repeat(self.args.val_query)
            if torch.cuda.is_available():
                label = label.type(torch.cuda.LongTensor)
            else:
                label = label.type(torch.LongTensor)

            # Print previous information
            if epoch % 10 == 0:
                print('Best Epoch {}, Best Meta-Val Acc={:.4f}'.format(trlog['max_acc_epoch'], trlog['max_acc']))
            # Run meta-validation
            for i, batch in enumerate(self.val_loader, 1):
                if torch.cuda.is_available():
                    data, _ = [_.cuda() for _ in batch]
                else:
                    data = batch[0]
                p = self.args.shot * self.args.way
                data_shot, data_query = data[:p], data[p:]
                del data
                logits = self.model((data_shot, label_shot, data_query))
                # Calculate loss and train accuracy/ train auc
                loss = F.cross_entropy(logits, label)
                acc = count_acc(logits, label)
                del logits
                val_loss_averager.add(loss.item())
                val_acc_averager.add(acc)
                del loss
                torch.cuda.empty_cache()
            # Update validation averagers
            val_loss_averager = val_loss_averager.item()
            val_acc_averager = val_acc_averager.item()
            # Write the tensorboardX records
            writer.add_scalar('data/meta_val_loss', float(val_loss_averager), epoch)
            writer.add_scalar('data/meta_val_acc', float(val_acc_averager), epoch)
            # Print loss and accuracy for this epoch
            print('Epoch {}, Val, Loss={:.4f} Acc={:.4f}'.format(epoch, val_loss_averager, val_acc_averager))

            # Update best saved model
            if val_acc_averager > trlog['max_acc']:
                trlog['max_acc'] = val_acc_averager
                trlog['max_acc_epoch'] = epoch
                self.save_model('max_acc')
            # Save model every 10 epochs
            if epoch % 10 == 0:
                self.save_model('epoch' + str(epoch))

            # Update the logs
            trlog['train_loss'].append(train_loss_averager)
            trlog['train_acc'].append(train_acc_averager)
            trlog['meta_val_loss'].append(val_loss_averager)
            trlog['meta_val_acc'].append(val_acc_averager)

            # Save log
            torch.save(trlog, osp.join(self.args.save_path, 'trlog'))

            if epoch % 10 == 0:
                print('Running Time: {}, Estimated Time: {}'.format(timer.measure(),
                                                                    timer.measure(epoch / self.args.max_epoch)))
        print("--------------End of meta train-------------")
        writer.close()

    def eval(self):
        """The function for the meta-eval phase."""

        # Load the logs
        def multiclass_roc_auc_score(y_test, y_pred, average="macro"):
            lb = LabelBinarizer()
            lb.fit(y_test)
            y_test = lb.transform(y_test)
            y_pred = lb.transform(y_pred)
            return roc_auc_score(y_test, y_pred, average=average)

        trlog = torch.load(osp.join(self.args.save_path, 'trlog'))

        # Load meta-test set
        args = self.args
        #
        if args.dataset == 'BNCI2015004':
            from dataloader.DataSetLoader_BNCI2015004 import DataSetLoader_BNCI2015004 as Dataset
        elif args.dataset == 'BNCI2014001':
            from dataloader.DataSetLoader_BNCI2014001 import DataSetLoader_BNCI2014001 as Dataset
        elif args.dataset == 'Schirrmeister2017':
            from dataloader.DataSetLoader_Schirrmeister2017 import DataSetLoader_Schirrmeister2017 as Dataset
        elif args.dataset == 'BNCI2014001_SPD':
            from dataloader.DataSetLoader_BNCI2014001_SPD import DataSetLoader_BNCI2014001_SPD as Dataset
        elif args.dataset == 'Schirrmeister2017_SPD':
            from dataloader.DataSetLoader_Schirrmeister2017_SPD import DataSetLoader_Schirrmeister2017_SPD as Dataset
        elif args.dataset == 'BNCI2015004_SPD':
            from dataloader.DataSetLoader_BNCI2015004_SPD import DataSetLoader_BNCI2015004_SPD as Dataset
        else:
            assert print('wrong dataset input')
        print('------meta-val after meta-train-----------------------------------------------------')
        print("Preparing meta-test set loader")
        print('test subject:',self.args.TestSubject[0])
        test_set = Dataset('test', self.args, TrainSubjects=self.args.TrainSubjects, ValSubject=self.args.ValSubject,
                           TestSubject=self.args.TestSubject, BinaryClassify=args.BinaryClassify)
        sampler = CategoriesSampler(test_set.label, 20, self.args.way, self.args.shot + self.args.val_query)
        loader = DataLoader(test_set, batch_sampler=sampler, num_workers=8, pin_memory=True)
        # Set test accuracy recorder
        test_acc_record = np.zeros((20,))  #
        test_f1_record = np.zeros((20,))
        test_auc_record = np.zeros((20,))
        # Load model for meta-test phase
        if self.args.eval_weights is not None:
            self.model.load_state_dict(torch.load(self.args.eval_weights)['params'])
        else:
            self.model.load_state_dict(torch.load(osp.join(self.args.save_path, 'max_acc' + '.pth'))['params'])
        # Set model to eval mode
        self.model.eval()

        # Set accuracy averager
        ave_acc = Averager()

        # Generate labels
        label = torch.arange(self.args.way).repeat(self.args.val_query)
        if torch.cuda.is_available():
            label = label.type(torch.cuda.LongTensor)
        else:
            label = label.type(torch.LongTensor)
        label_shot = torch.arange(self.args.way).repeat(self.args.shot)
        if torch.cuda.is_available():
            label_shot = label_shot.type(torch.cuda.LongTensor)
        else:
            label_shot = label_shot.type(torch.LongTensor)

        Y = label.data.cpu().numpy()
        # Start meta-test
        for i, batch in enumerate(loader, 1):  ##
            if torch.cuda.is_available():
                data, _ = [_.cuda() for _ in batch]
            else:
                data = batch[0]
            k = self.args.way * self.args.shot
            data_shot, data_query = data[:k], data[k:]
            logits = self.model((data_shot, label_shot, data_query))
            acc = count_acc(logits, label)
            logits = logits.data.cpu().numpy()  ##
            predicted = np.argmax(logits, axis=1)
            f1 = f1_score(Y, predicted, average='macro')
            auc = multiclass_roc_auc_score(Y, predicted)  ##
            ave_acc.add(acc)
            test_acc_record[i - 1] = acc
            test_f1_record[i - 1] = f1  #
            test_auc_record[i - 1] = auc
            if i % 100 == 0:
                print('batch {}: {:.2f}({:.2f})'.format(i, ave_acc.item() * 100, acc * 100))

        # Calculate the confidence interval, update the logs
        m, pm = compute_confidence_interval(test_acc_record)
        f1_m, f1_pm = compute_confidence_interval(test_f1_record)
        auc_m, auc_pm = compute_confidence_interval(test_auc_record)

        print('Val Best Epoch {}, Acc {:.4f}, Test Acc {:.4f}'.format(trlog['max_acc_epoch'], trlog['max_acc'],
                                                                      ave_acc.item()))
        print('Test Acc {:.4f} + {:.4f}'.format(m, pm))
        print('Test f1 {:.4f} + {:.4f}'.format(f1_m, f1_pm))
        print('Test auc {:.4f} + {:.4f}'.format(auc_m, auc_pm))



