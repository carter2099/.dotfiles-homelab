cd hub
RAILS_MASTER_KEY=$(cat rails-api/config/master.key) docker compose -f docker-compose.yml up -d
