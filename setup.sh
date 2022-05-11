pip uninstall torch torchvision torchtext anomalib -y
pip install torch torchvision --extra-index-url https://download.pytorch.org/whl/cu113
# pip install torch==1.8.2+cu111 torchvision==0.9.2+cu111 -f https://download.pytorch.org/whl/lts/1.8/torch_lts.html
pip install -r requirements/base.txt
pip install -r requirements/dev.txt
pip install -r requirements/openvino.txt
pip install -e .