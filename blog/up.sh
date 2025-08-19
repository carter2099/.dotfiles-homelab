cd blog
RAILS_MASTER_KEY=$(cat config/master.key) docker compose -f docker-compose.prod.yml up -d
