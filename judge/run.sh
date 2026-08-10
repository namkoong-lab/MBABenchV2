#!/bin/bash
## Source project configurations and change to script directory. Then run Python script with passed arguments.

current_dir=$(pwd)

cd $(dirname "$0")
source ./project_configs.sh

cd $current_dir

python "$@"
