#! /bin/bash

cd stickies

echo 'Pulling changes from remote'
git pull

echo 'Bringing apps down...'
docker-compose down

echo 'Starting apps back up'
docker-compose -f docker-compose.prod.yml up --build -d

echo 'Release complete'
