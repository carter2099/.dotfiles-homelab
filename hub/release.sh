#! /bin/bash

cd hub

echo 'Pulling changes from remote'
git pull

echo 'Bringing apps down...'
RAILS_MASTER_KEY=$(cat rails-api/config/master.key) docker compose -f docker-compose.yml down
docker image rm hub-server

echo 'Starting app back up'
cd ..
./up.sh

echo 'Release complete'
