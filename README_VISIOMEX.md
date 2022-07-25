## Env setup
```bash
conda create -n anomalib python=3.10
conda activate anomalib
pip install --upgrade pip
pip install anomalib==0.3.3
pip install torch=="1.11.0+cu115" torchvision=="0.12.0+cu115" torchtext==0.12.0 --extra-index-url https://download.pytorch.org/whl/cu115 --upgrade --force-reinstall
#conda install pytorch torchvision cudatoolkit=11.6 -c pytorch -c conda-forge
```

## Config modifications for visiomex
```bash
...
path: ../datasets/Visiomex
category: kas
...
num_workers: 16
```
