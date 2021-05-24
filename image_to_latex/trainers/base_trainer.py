import time
from typing import Any, Dict, List, Optional, Sequence, Union

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from tqdm import tqdm

import wandb
from image_to_latex.models.base_model import BaseModel
from image_to_latex.utils.lr_finder import LRFinder_
from image_to_latex.utils.metrics import bleu_score, edit_distance
from image_to_latex.utils.misc import compute_time_elapsed


MAX_EPOCHS = 100
PATIENCE = 10
LR = 1e-4
MAX_LR = 1e-2
SAVE_BEST_MODEL = False
USE_SCHEDULER = False

CHECKPOINT_FILENAME = "best.pth"


class BaseTrainer:
    """Specify every aspect of training.

    Args:
        model: The model to be fitted.
        config: Configurations passed from command line.
        wandb_run: An instance of a Weights & Biases run.

    Attributes:
        max_epochs: Maximum number of epochs to run.
        patience: Number of epochs with no improvement before stopping the
            training. Use -1 to disable early stopping.
        lr: Learning rate.
        max_lr: Maximum learning rate to use in one-cycle learning rate
            scheduler. Use -1 to to run learning rate range test. Ignored if
            `use_scheduler` is False.
        save_best_model: Save a checkpoint when the current model has the best
            validation loss so far.
        use_scheduler: Specifies whether to use learning rate scheduler or not.
        start_epoch: The first epoch number.
        best_val_loss: Best validation loss encountered so far.
        no_improve_count: Number of epochs since the last improvement in
            validation loss.
        device: Which device to put the model and data in.
        criterion: Loss function.
        optimizer: Optimization algorithm to use.
        scheduler: Learning rate scheduler.
        checkpoint: State dict for model.
    """

    def __init__(
        self,
        model: BaseModel,
        config: Dict[str, Any],
        wandb_run: Optional[wandb.sdk.wandb_run.Run] = None,
    ) -> None:
        self.model = model
        self.wandb_run = wandb_run

        self.max_epochs = config.get("max-epochs", MAX_EPOCHS)
        self.patience = config.get("patience", PATIENCE)
        self.lr = config.get("lr", LR)
        self.max_lr = config.get("max-lr", MAX_LR)
        self.save_best_model = config.get("save-best-model", SAVE_BEST_MODEL)
        self.use_scheduler = config.get("use-scheduler", USE_SCHEDULER)

        self.tokenizer = self.model.tokenizer
        self.start_epoch = 1
        self.best_val_loss = float("inf")
        self.no_improve_count = 0
        self.device = torch.device(
            "cuda" if torch.cuda.is_available() else "cpu"
        )

        self.criterion: Union[nn.CrossEntropyLoss, nn.CTCLoss]
        self.optimizer: optim.Optimizer
        self.scheduler: optim.lr_scheduler._LRScheduler
        self.checkpoint: Dict[str, torch.Tensor]

    def config(self) -> Dict[str, Any]:
        """Returns important configuration for reproducibility."""
        return {
            "max-epochs": self.max_epochs,
            "patience": self.patience,
            "lr": self.lr,
            "max-lr": self.max_lr,
            "use-scheduler": self.use_scheduler,
            "save-best-model": self.save_best_model,
        }

    def fit(
        self,
        train_dataloader: DataLoader,
        val_dataloader: DataLoader,
    ) -> None:
        """Specify what happens during training."""
        # Configure optimizier
        self.optimizer = optim.Adam(self.model.parameters(), lr=1e-6)

        # Configure scheduler
        if self.use_scheduler:
            # Find maximum learning rate
            if self.max_lr < 0:
                print("Running learning rate range test...")
                self.max_lr = self._find_optimal_lr(train_dataloader)

            self.scheduler = optim.lr_scheduler.OneCycleLR(  # type: ignore
                self.optimizer,
                max_lr=self.max_lr,
                epochs=self.max_epochs,
                steps_per_epoch=len(train_dataloader),
                pct_start=0.5,
                div_factor=10,
                final_div_factor=1e4,
            )

        # For display purpose
        width = len(str(self.max_epochs))

        data_loaders = {"train": train_dataloader, "val": val_dataloader}
        self.model.to(self.device)

        for epoch in range(self.start_epoch, self.max_epochs + 1):
            avg_loss = {"train": 0.0, "val": 0.0}
            start_time = time.time()
            for phase in ["train", "val"]:
                total_loss = 0.0
                if phase == "train":
                    self.model.train()
                else:
                    self.model.eval()
                pbar = tqdm(data_loaders[phase], desc=phase, leave=False)
                for batch in pbar:
                    batch = self._move_to_device(batch)
                    if phase == "train":
                        loss = self.training_step(batch)
                        loss.backward()
                        self.optimizer.step()
                        if self.use_scheduler:
                            self.scheduler.step()
                    else:
                        loss = self.validation_step(batch)
                    total_loss += loss.item()
                    pbar.set_postfix({f"{phase}_loss": loss.item()})
                avg_loss[phase] = total_loss / len(data_loaders[phase])
            end_time = time.time()
            mins, secs = compute_time_elapsed(start_time, end_time)

            # Print training progress
            print(
                f"Epoch {epoch:{width}d}/{self.max_epochs} | "
                f"Train loss: {avg_loss['train']:.3f} | "
                f"Val loss: {avg_loss['val']:.3f} | "
                f"Time: {mins}m {secs}s"
            )

            # Early stopping and save checkpoint
            if self._early_stopping(avg_loss["val"]):
                print(
                    f"Training is terminated because validation loss has "
                    f"stopped decreasing for {self.patience} epochs.\n"
                )
                break

        if self.wandb_run:
            wandb.run.summary["epoch"] = min(epoch, self.max_epochs)  # type: ignore  # noqa: E501

    def training_step(self, batch: Sequence):
        """Training step."""
        imgs, targets = batch
        logits = self.model(imgs, targets)
        loss = self.criterion(logits, targets)
        return loss

    @torch.no_grad()
    def validation_step(self, batch: Sequence):
        """Validation step."""
        imgs, targets = batch
        logits = self.model(imgs, targets)
        loss = self.criterion(logits, targets)
        return loss

    @torch.no_grad()
    def test(self, test_dataloader: DataLoader) -> None:
        """Specify what happens during testing."""
        if self.save_best_model:
            self.model.load_state_dict(self.checkpoint)  # type: ignore

        references: List[List[str]] = []
        hypothesis: List[List[str]] = []

        self.model.to(self.device)
        self.model.eval()

        pbar = tqdm(test_dataloader, desc="Testing: ", leave=False)
        for batch in pbar:
            batch = self._move_to_device(batch)
            imgs, targets = batch
            preds = self.model.predict(imgs)
            references += self.tokenizer.unindex(
                targets.tolist(), inference=True
            )
            hypothesis += self.tokenizer.unindex(
                preds.tolist(), inference=True
            )
        bleu = bleu_score(references, hypothesis) * 100
        ed = edit_distance(references, hypothesis) * 100
        print(
            "Evaluation Results:\n"
            "====================\n"
            f"BLEU: {bleu:.3f}\n"
            f"Edit Distance: {ed:.3f}\n"
            "====================\n"
        )
        if self.wandb_run:
            wandb.run.summary["bleu"] = bleu  # type: ignore
            wandb.run.summary["edit_distance"] = ed  # type: ignore

    def _move_to_device(self, batch: Sequence) -> List[Any]:
        """Move tensors to device."""
        return [
            x.to(self.device) if isinstance(x, torch.Tensor) else x
            for x in batch
        ]

    def _early_stopping(self, current_val_loss: float) -> bool:
        """Returns whether the training should stop."""
        if current_val_loss < self.best_val_loss:
            self.best_val_loss = current_val_loss
            self.no_improve_count = 0
            self._save_checkpoint()
        else:
            self.no_improve_count += 1
            if self.no_improve_count == self.patience:
                return True
        return False

    def _save_checkpoint(self) -> None:
        """Save a checkpoint to be used for inference."""
        if not self.save_best_model:
            return
        self.checkpoint = self.model.state_dict()

    def _find_optimal_lr(self, dataloader: DataLoader) -> Optional[float]:
        """Returns suggested learning rate."""
        lr_finder = LRFinder_(self.model, self.optimizer, self.criterion)
        lr_finder.range_test(dataloader, end_lr=100, num_iter=100)
        max_lr = lr_finder.suggest_lr()
        return max_lr