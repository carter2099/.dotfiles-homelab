#! /bin/bash

cd blog

echo 'Pulling changes from remote'
git pull

echo 'Bringing app down...'
docker-compose down

echo 'Starting app back up'
../up.sh

echo 'Release complete'
