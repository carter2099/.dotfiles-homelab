#! /bin/bash

cd blog

echo 'Pulling changes from remote'
git pull

echo 'Bringing app down...'
RAILS_MASTER_KEY=$(cat config/master.key) docker compose -f docker-compose.prod.yml down
docker image rm blog-web

echo 'Starting app back up'
cd ..
./up.sh

echo 'Release complete'
