import os
import numpy as np
import pandas as pd
from sklearn import metrics
import torch
import torch.nn as nn
import pytorch_lightning as pl

import transformers
from transformers import AdamW, get_cosine_schedule_with_warmup


class MainModel(nn.Module):
    def __init__(self, args=None, **kwargs):
        super().__init__()
        self.base = transformers.AutoModel.from_pretrained(args.base_path)
        self.dropout = nn.Dropout(0.3)
        self.linear = nn.Linear(768,1)

    def forward(self, ids_seq, attn_masks, token_type_ids=None):
        base_out = self.base(
            ids_seq, attention_mask=attn_masks, token_type_ids=token_type_ids
        )
        # using maxpooled output
        max_out = self.dropout(base_out[1])
        return self.linear(max_out)


class SequenceClassicationLightningModule(pl.LightningModule):
    def __init__(self, args, **kwargs):
        super().__init__()

        self.save_hyperparameters(args)
        self.model = MainModel(self.hparams)

    @staticmethod
    def loss(logits, targets):
        return nn.BCEWithLogitsLoss()(logits, targets)

    def shared_step(self, batch):
        ids_seq, attn_masks, target = (
            batch["ids_seq"],
            batch["attn_masks"],
            batch["target"],
        )
        logits = self.model(ids_seq, attn_masks).squeeze()
        loss = self.loss(logits, target)
        return logits, loss

    def training_step(self, batch, batch_idx):
        logits, loss = self.shared_step(batch)
        self.log(
            "train_loss", loss, on_step=True, on_epoch=True, prog_bar=False, logger=True
        )
        return {"loss": loss, "logits": logits, "true_preds": batch["target"]}

    def validation_step(self, batch, batch_idx):
        logits, loss = self.shared_step(batch)
        self.log(
            "valid_loss", loss, on_step=False, on_epoch=True, prog_bar=False, logger=True,
        )
        return {"logits": logits, "true_preds": batch["target"]}

    def configure_optimizers(self):
        grouped_parameters = [
            {"params": self.model.base.parameters(), "lr": self.hparams.base_lr},
            {"params": self.model.linear.parameters(), "lr": self.hparams.linear_lr},
        ]
        optim = AdamW(grouped_parameters, lr=self.hparams.base_lr)
        return optim
    
    def training_epoch_end(self, training_step_outputs):
        y_pred = torch.sigmoid(torch.cat([out["logits"] for out in training_step_outputs])).to("cpu").detach().numpy() >= 0.5
        y_true = torch.cat([out["true_preds"] for out in training_step_outputs]).to("cpu", dtype=int).detach().numpy()
        
        acc = metrics.accuracy_score(y_pred, y_true)
        f1 = metrics.f1_score(y_pred, y_true)
        
        self.log("train_acc", acc)
        self.log("train_f1", f1)
    
    def validation_epoch_end(self, validation_step_outputs):
        y_pred = torch.sigmoid(torch.cat([out["logits"] for out in validation_step_outputs])).to("cpu").detach().numpy() >= 0.5
        y_true = torch.cat([out["true_preds"] for out in validation_step_outputs]).to("cpu", dtype=int).detach().numpy()
        
        acc = metrics.accuracy_score(y_pred, y_true)
        f1 = metrics.f1_score(y_pred, y_true)
        
        self.log("val_acc", acc)
        self.log("val_f1", f1)


class LightningModuleForTokenClassification(pl.LightningModule):
    def __init__(self, args, **kwargs):
        super().__init__()

        self.save_hyperparameters(args)
        self.model = transformers.AutoModelForTokenClassification.from_pretrained(self.hparams.base_path, num_labels=2)

    def training_step(self, batch, batch_idx):
        output = self.model(**batch)
        self.log(
            "train_loss", output['loss'].item(), on_step=True, on_epoch=True, prog_bar=True, logger=True
        )
        return {"loss": output['loss'], "logits": output['logits'], "true_preds": batch["labels"]}

    def validation_step(self, batch, batch_idx):
        output = self.model(**batch)
        self.log(
            "valid_loss", output['loss'].item(), on_step=False, on_epoch=True, prog_bar=True, logger=True,
        )
        return {"logits": output['logits'], "true_preds": batch["labels"]}
    
    def test_step(self, batch, batch_idx):
        output = self.model(**batch)
        self.log(
            "test_loss", output['loss'].item(), on_step=False, on_epoch=True, prog_bar=True, logger=True,
        )
        return {"logits": output['logits'], "true_preds": batch["labels"]}

    def configure_optimizers(self):
        params = list(self.model.named_parameters())
        
        def is_backbone(n): return 'classifier' not in n
        
        grouped_parameters = [
            {"params": [p for n,p in params if is_backbone(n)], "lr": self.hparams.base_lr},
            {"params": [p for n,p in params if not is_backbone(n)], "lr": self.hparams.linear_lr},
        ]
        optim = AdamW(grouped_parameters, lr=self.hparams.base_lr)
        return optim
    
    def training_epoch_end(self, training_step_outputs):
        
        y_pred = torch.cat([torch.argmax(out["logits"], dim=-1).view(-1) for out in training_step_outputs]).to("cpu").detach().numpy().reshape(-1)
        y_true = torch.cat([out["true_preds"].view(-1) for out in training_step_outputs]).to("cpu", dtype=int).detach().numpy().reshape(-1)
        
        mask = y_true != -100
        y_true = y_true[mask]
        y_pred = y_pred[mask]
        
        acc = metrics.accuracy_score(y_true, y_pred)
        f1 = metrics.f1_score(y_true, y_pred, average="weighted")
        
        self.log("train_acc", acc)
        self.log("train_f1", f1)
    
    def validation_epoch_end(self, validation_step_outputs):

        y_pred = torch.cat([torch.argmax(out["logits"], dim=-1).view(-1) for out in validation_step_outputs]).to("cpu").detach().numpy().reshape(-1)
        y_true = torch.cat([out["true_preds"].view(-1) for out in validation_step_outputs]).to("cpu", dtype=int).detach().numpy().reshape(-1)

        mask = y_true != -100

        acc = metrics.accuracy_score(y_true, y_pred)
        f1 = metrics.f1_score(y_true, y_pred, average="weighted")

        self.log("val_acc", acc)
        self.log("val_f1", f1)
    
    def test_epoch_end(self, test_step_outputs):

        y_pred = torch.cat([torch.argmax(out["logits"], dim=-1).view(-1) for out in test_step_outputs]).to("cpu").detach().numpy().reshape(-1)
        y_true = torch.cat([out["true_preds"].view(-1) for out in test_step_outputs]).to("cpu", dtype=int).detach().numpy().reshape(-1)

        mask = y_true != -100

        acc = metrics.accuracy_score(y_true, y_pred)
        f1 = metrics.f1_score(y_true, y_pred, average="weighted")

        self.log("test_acc", acc)
        self.log("test_f1", f1)
        
        
class LightningModuleForAutoModels(pl.LightningModule):
    def __init__(self, args, **kwargs):
        super().__init__()

        self.save_hyperparameters(args)
        self.model = transformers.AutoModelForSequenceClassification.from_pretrained(self.hparams.base_path, num_labels=self.hparams.num_labels)

    @staticmethod
    def loss_fn(logits, targets):
        ce = torch.nn.CrossEntropyLoss(weight=torch.tensor([0.30,1.,0.10]))
        return ce(logits, targets)
    
    def training_step(self, batch, batch_idx):
        output = self.model(**batch)
        self.log(
            "train_loss", output['loss'].item(), on_step=True, on_epoch=True, prog_bar=True, logger=True
        )
        return {"loss": output['loss'], "logits": output['logits'], "true_preds": batch["labels"]}

    def validation_step(self, batch, batch_idx):
        output = self.model(**batch)
        self.log(
            "valid_loss", output['loss'].item(), on_step=False, on_epoch=True, prog_bar=True, logger=True,
        )
        return {"logits": output['logits'], "true_preds": batch["labels"]}
    
    def test_step(self, batch, batch_idx):
        output = self.model(**batch)
        self.log(
            "test_loss", output['loss'].item(), on_step=False, on_epoch=True, prog_bar=True, logger=True,
        )
        return {"logits": output['logits'], "true_preds": batch["labels"]}

    def configure_optimizers(self):
        params = list(self.model.named_parameters())
        
        def is_backbone(n): return 'classifier' not in n
        
        grouped_parameters = [
            {"params": [p for n,p in params if is_backbone(n)], "lr": self.hparams.base_lr},
            {"params": [p for n,p in params if not is_backbone(n)], "lr": self.hparams.linear_lr},
        ]
        optim = AdamW(grouped_parameters, lr=self.hparams.base_lr)
        return optim
    
    def training_epoch_end(self, training_step_outputs):
        
        y_pred = torch.cat([torch.argmax(out["logits"], dim=-1).view(-1) for out in training_step_outputs]).to("cpu").detach().numpy().reshape(-1)
        y_true = torch.cat([out["true_preds"].view(-1) for out in training_step_outputs]).to("cpu", dtype=int).detach().numpy().reshape(-1)
        
        mask = y_true != -100
        y_true = y_true[mask]
        y_pred = y_pred[mask]
        
        acc = metrics.accuracy_score(y_true, y_pred)
        f1 = metrics.f1_score(y_true, y_pred, average="weighted")
        
        self.log("train_acc", acc)
        self.log("train_f1", f1)
    
    def validation_epoch_end(self, validation_step_outputs):

        y_pred = torch.cat([torch.argmax(out["logits"], dim=-1).view(-1) for out in validation_step_outputs]).to("cpu").detach().numpy().reshape(-1)
        y_true = torch.cat([out["true_preds"].view(-1) for out in validation_step_outputs]).to("cpu", dtype=int).detach().numpy().reshape(-1)

        mask = y_true != -100
        y_true = y_true[mask]
        y_pred = y_pred[mask]

        acc = metrics.accuracy_score(y_true, y_pred)
        f1 = metrics.f1_score(y_true, y_pred, average="weighted")

        print(f"==> val_acc: {acc}, val_f1: {f1}")
        self.log("val_acc", acc)
        self.log("val_f1", f1)
    
    def test_epoch_end(self, test_step_outputs):

        y_pred = torch.cat([torch.argmax(out["logits"], dim=-1).view(-1) for out in test_step_outputs]).to("cpu").detach().numpy().reshape(-1)
        y_true = torch.cat([out["true_preds"].view(-1) for out in test_step_outputs]).to("cpu", dtype=int).detach().numpy().reshape(-1)

        mask = y_true != -100
        y_true = y_true[mask]
        y_pred = y_pred[mask]

        acc = metrics.accuracy_score(y_true, y_pred)
        f1 = metrics.f1_score(y_true, y_pred, average="weighted")

        print(f"==> test_acc: {acc}, test_f1: {f1}")
        self.log("test_acc", acc)
        self.log("test_f1", f1)        
        
if __name__ == "__main__":
    pass
