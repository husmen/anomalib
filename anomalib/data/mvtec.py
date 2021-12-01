"""MVTec Dataset.

MVTec This script contains PyTorch Dataset, Dataloader and PyTorch
Lightning DataModule for the MVTec dataset.

If the dataset is not on the file system, the script downloads and
extracts the dataset and create PyTorch data objects.
"""

# Copyright (C) 2020 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.

import logging
import random
import tarfile
from abc import ABC
from pathlib import Path
from typing import Dict, Optional, Tuple, Union
from urllib.request import urlretrieve

import albumentations as A
import cv2
import numpy as np
import pandas as pd
from pandas.core.frame import DataFrame
from pytorch_lightning.core.datamodule import LightningDataModule
from pytorch_lightning.utilities.cli import DATAMODULE_REGISTRY
from torch import Tensor
from torch.utils.data import DataLoader
from torch.utils.data.dataset import Dataset
from torchvision.datasets.folder import VisionDataset

from anomalib.data.transforms import PreProcessor
from anomalib.data.utils import read_image
from anomalib.utils.download_progress_bar import DownloadProgressBar

logger = logging.getLogger(name="Dataset: MVTec")
logger.setLevel(logging.DEBUG)

__all__ = ["Mvtec"]


def split_normal_images_in_train_set(samples: DataFrame, split_ratio: float = 0.1, seed: int = 0) -> DataFrame:
    """This function splits the normal images in training set and assigns the values to the test set.

    This is particularly useful especially when the test set does not contain any normal images.
    This is important because when the test set doesn't have any normal images,
    AUC computation fails due to having single class.

    Args:
        samples (DataFrame): Dataframe containing dataset info such as filenames, splits etc.
        split_ratio (float, optional): Train-Test normal image split ratio. Defaults to 0.1.
        seed (int, optional): Random seed to ensure reproducibility. Defaults to 0.

    Returns:
        DataFrame: Output dataframe where the part of the training set is assigned to test set.
    """
    random.seed(seed)

    normal_train_image_indices = samples.index[(samples.split == "train") & (samples.label == "good")].to_list()
    num_normal_train_images = len(normal_train_image_indices)
    num_normal_valid_images = int(num_normal_train_images * split_ratio)

    indices_to_split_from_train_set = random.sample(population=normal_train_image_indices, k=num_normal_valid_images)
    samples.loc[indices_to_split_from_train_set, "split"] = "test"

    return samples


def make_mvtec_dataset(path: Path, split: str = "train", split_ratio: float = 0.1, seed: int = 0) -> DataFrame:
    """Create MVTec samples by parsing the MVTec data file structure.

    The files are expected to follow the structure:
        path/to/dataset/split/category/image_filename.png
        path/to/dataset/ground_truth/category/mask_filename.png

    This function creates a dataframe to store the parsed information based on the following format:
    |---|---------------|-------|---------|---------------|---------------------------------------|-------------|
    |   | path          | split | label   | image_path    | mask_path                             | label_index |
    |---|---------------|-------|---------|---------------|---------------------------------------|-------------|
    | 0 | datasets/name |  test |  defect |  filename.png | ground_truth/defect/filename_mask.png | 1           |
    |---|---------------|-------|---------|---------------|---------------------------------------|-------------|

    Args:
        path (Path): Path to dataset
        split (str, optional): Dataset split (ie., either train or test). Defaults to "train".
        split_ratio (float, optional): Ratio to split normal training images and add to the
                                       test set in case test set doesn't contain any normal images.
                                       Defaults to 0.1.
        seed (int, optional): Random seed to ensure reproducibility when splitting. Defaults to 0.

    Example:
        The following example shows how to get training samples from MVTec bottle category:

        >>> root = Path('./MVTec')
        >>> category = 'bottle'
        >>> path = root / category
        >>> path
        PosixPath('MVTec/bottle')

        >>> samples = make_mvtec_dataset(path, split='train', split_ratio=0.1, seed=0)
        >>> samples.head()
           path         split label image_path                           mask_path                   label_index
        0  MVTec/bottle train good MVTec/bottle/train/good/105.png MVTec/bottle/ground_truth/good/105_mask.png 0
        1  MVTec/bottle train good MVTec/bottle/train/good/017.png MVTec/bottle/ground_truth/good/017_mask.png 0
        2  MVTec/bottle train good MVTec/bottle/train/good/137.png MVTec/bottle/ground_truth/good/137_mask.png 0
        3  MVTec/bottle train good MVTec/bottle/train/good/152.png MVTec/bottle/ground_truth/good/152_mask.png 0
        4  MVTec/bottle train good MVTec/bottle/train/good/109.png MVTec/bottle/ground_truth/good/109_mask.png 0

    Returns:
        DataFrame: an output dataframe containing samples for the requested split (ie., train or test)
    """
    samples_list = [(str(path),) + filename.parts[-3:] for filename in path.glob("**/*.png")]
    if len(samples_list) == 0:
        raise RuntimeError(f"Found 0 images in {path}")

    samples = pd.DataFrame(samples_list, columns=["path", "split", "label", "image_path"])
    samples = samples[samples.split != "ground_truth"]

    # Create mask_path column
    samples["mask_path"] = (
        samples.path
        + "/ground_truth/"
        + samples.label
        + "/"
        + samples.image_path.str.rstrip("png").str.rstrip(".")
        + "_mask.png"
    )

    # Modify image_path column by converting to absolute path
    samples["image_path"] = samples.path + "/" + samples.split + "/" + samples.label + "/" + samples.image_path

    # Split the normal images in training set if test set doesn't
    # contain any normal images. This is needed because AUC score
    # cannot be computed based on 1-class
    if sum((samples.split == "test") & (samples.label == "good")) == 0:
        samples = split_normal_images_in_train_set(samples, split_ratio, seed)

    # Good images don't have mask
    samples.loc[(samples.split == "test") & (samples.label == "good"), "mask_path"] = ""

    # Create label index for normal (0) and anomalous (1) images.
    samples.loc[(samples.label == "good"), "label_index"] = 0
    samples.loc[(samples.label != "good"), "label_index"] = 1
    samples.label_index = samples.label_index.astype(int)

    # Get the data frame for the split.
    samples = samples[samples.split == split]
    samples = samples.reset_index(drop=True)

    return samples


class _MvtecDataset(VisionDataset):
    """MVTec PyTorch Dataset."""

    def __init__(
        self,
        root: Union[Path, str],
        category: str,
        pre_process: PreProcessor,
        task: str = "segmentation",
        is_train: bool = True,
        download: bool = False,
    ) -> None:
        """Mvtec Dataset class.

        Args:
            root: Path to the MVTec dataset
            category: Name of the MVTec category.
            pre_process: List of pre_processing object containing albumentation compose.
            task: ``classification`` or ``segmentation``
            is_train: Boolean to check if the split is training
            download: Boolean to download the MVTec dataset.

        Examples:
            >>> from anomalib.data.mvtec import _MvtecDataset
            >>> from anomalib.data.transforms import PreProcessor
            >>> pre_process = PreProcessor(image_size=256)
            >>> dataset = _MvtecDataset(
            ...     root='./datasets/MVTec',
            ...     category='leather',
            ...     pre_process=pre_process,
            ...     task="classification",
            ...     is_train=True,
            ... )
            >>> dataset[0].keys()
            dict_keys(['image'])

            >>> dataset.split = "test"
            >>> dataset[0].keys()
            dict_keys(['image', 'image_path', 'label'])

            >>> dataset.task = "segmentation"
            >>> dataset.split = "train"
            >>> dataset[0].keys()
            dict_keys(['image'])

            >>> dataset.split = "test"
            >>> dataset[0].keys()
            dict_keys(['image_path', 'label', 'mask_path', 'image', 'mask'])

            >>> dataset[0]["image"].shape, dataset[0]["mask"].shape
            (torch.Size([3, 256, 256]), torch.Size([256, 256]))
        """
        super().__init__(root)
        self.root = Path(root) if isinstance(root, str) else root
        self.category: str = category
        self.split = "train" if is_train else "test"
        self.task = task

        self.pre_process = pre_process

        if download:
            self._download()

        self.samples = make_mvtec_dataset(path=self.root / category, split=self.split)

    def _download(self) -> None:
        """Download the MVTec dataset."""
        if (self.root / self.category).is_dir():
            logger.warning("Dataset directory exists.")
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            dataset_name = "mvtec_anomaly_detection.tar.xz"
            self.filename = self.root / dataset_name

            logger.info("Downloading MVTec Dataset")
            with DownloadProgressBar(unit="B", unit_scale=True, miniters=1, desc=dataset_name) as progress_bar:
                urlretrieve(
                    url=f"ftp://guest:GU.205dldo@ftp.softronics.ch/mvtec_anomaly_detection/{dataset_name}",
                    filename=self.filename,
                    reporthook=progress_bar.update_to,
                )

            self._extract()
            self._clean()

    def _extract(self) -> None:
        """Extract MVTec Dataset."""
        logger.info("Extracting MVTec dataset")
        with tarfile.open(self.filename) as file:
            file.extractall(self.root)

    def _clean(self) -> None:
        """Cleanup MVTec Dataset tar file."""
        logger.info("Cleaning up the tar file")
        self.filename.unlink()

    def __len__(self) -> int:
        """Get length of the dataset."""
        return len(self.samples)

    def __getitem__(self, index: int) -> Dict[str, Union[str, Tensor]]:
        """Get dataset item for the index ``index``.

        Args:
            index (int): Index to get the item.

        Returns:
            Union[Dict[str, Tensor], Dict[str, Union[str, Tensor]]]: Dict of image tensor during training.
                Otherwise, Dict containing image path, target path, image tensor, label and transformed bounding box.
        """
        item: Dict[str, Union[str, Tensor]] = {}

        image_path = self.samples.image_path[index]
        image = read_image(image_path)

        if self.split == "train" or self.task == "classification":
            pre_processed = self.pre_process(image=image)
            item = {"image": pre_processed["image"]}

        if self.split == "test":
            label_index = self.samples.label_index[index]

            item["image_path"] = image_path
            item["label"] = label_index

            if self.task == "segmentation":
                mask_path = self.samples.mask_path[index]

                # Only Anomalous (1) images has masks in MVTec dataset.
                # Therefore, create empty mask for Normal (0) images.
                if label_index == 0:
                    mask = np.zeros(shape=image.shape[:2])
                else:
                    mask = cv2.imread(mask_path, flags=0) / 255.0

                pre_processed = self.pre_process(image=image, mask=mask)

                item["mask_path"] = mask_path
                item["image"] = pre_processed["image"]
                item["mask"] = pre_processed["mask"]

        return item


@DATAMODULE_REGISTRY
class Mvtec(LightningDataModule):
    """MVTec Lightning Data Module."""

    def __init__(
        self,
        root: str,
        category: str,
        # TODO: Remove default values. IAAALD-211
        image_size: Optional[Union[int, Tuple[int, int]]],
        train_batch_size: int = 32,
        test_batch_size: int = 32,
        num_workers: int = 8,
        transform_config: Optional[Union[str, A.Compose]] = None,
    ) -> None:
        """Mvtec Lightning Data Module.

        Args:
            root: Path to the MVTec dataset
            category: Name of the MVTec category.
            image_size: Variable to which image is resized.
            train_batch_size: Training batch size.
            test_batch_size: Testing batch size.
            num_workers: Number of workers.
            transform_config: Config for pre-processing.

        Examples
            >>> from anomalib.data import Mvtec
            >>> datamodule = Mvtec(
            ...     root="./datasets/MVTec",
            ...     category="leather",
            ...     image_size=256,
            ...     train_batch_size=32,
            ...     test_batch_size=32,
            ...     num_workers=8,
            ...     transform_config=None,
            ... )
            >>> datamodule.setup()

            >>> i, data = next(enumerate(datamodule.train_dataloader()))
            >>> data.keys()
            dict_keys(['image'])
            >>> data["image"].shape
            torch.Size([32, 3, 256, 256])

            >>> i, data = next(enumerate(datamodule.val_dataloader()))
            >>> data.keys()
            dict_keys(['image_path', 'label', 'mask_path', 'image', 'mask'])
            >>> data["image"].shape, data["mask"].shape
            (torch.Size([32, 3, 256, 256]), torch.Size([32, 256, 256]))
        """
        super().__init__()

        self.root = root if isinstance(root, Path) else Path(root)
        self.category = category
        self.dataset_path = self.root / self.category

        self.pre_process = PreProcessor(config=transform_config, image_size=image_size)

        self.train_batch_size = train_batch_size
        self.test_batch_size = test_batch_size
        self.num_workers = num_workers

        self.train_data: Dataset
        self.val_data: Dataset

    def prepare_data(self):
        """Prepare MVTec Dataset."""
        # Train
        _MvtecDataset(
            root=self.root,
            category=self.category,
            pre_process=self.pre_process,
            is_train=True,
            download=True,
        )

        # Test
        _MvtecDataset(
            root=self.root,
            category=self.category,
            pre_process=self.pre_process,
            is_train=False,
            download=True,
        )

    def setup(self, stage: Optional[str] = None) -> None:
        """Setup train, validation and test data.

        Args:
          stage: Optional[str]:  Train/Val/Test stages. (Default value = None)
        """
        self.val_data = _MvtecDataset(
            root=self.root,
            category=self.category,
            pre_process=self.pre_process,
            is_train=False,
        )
        if stage in (None, "fit"):
            self.train_data = _MvtecDataset(
                root=self.root,
                category=self.category,
                pre_process=self.pre_process,
                is_train=True,
            )

    def train_dataloader(self) -> DataLoader:
        """Get train dataloader."""
        return DataLoader(
            self.train_data, shuffle=False, batch_size=self.train_batch_size, num_workers=self.num_workers
        )

    def val_dataloader(self) -> DataLoader:
        """Get validation dataloader."""
        return DataLoader(self.val_data, shuffle=False, batch_size=self.test_batch_size, num_workers=self.num_workers)

    def test_dataloader(self) -> DataLoader:
        """Get test dataloader."""
        return DataLoader(self.val_data, shuffle=False, batch_size=self.test_batch_size, num_workers=self.num_workers)
