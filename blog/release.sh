#! /bin/bash

cd blog

echo 'Pulling changes from remote'
git pull

echo 'Bringing app down...'
docker-compose -f docker-compose.prod.yml down
docker image rm blog-web

echo 'Starting app back up'
cd ..
./up.sh

echo 'Release complete'
