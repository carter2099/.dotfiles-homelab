TOKEN=$(cat token)
curl \
--request PUT \
--url https://api.cloudflare.com/client/v4/zones/8a7bf3d7e6a0502b49aab39388f95417/dns_records/1384e69e61be8366827a9c2dc95e05be \
--header 'Content-Type: application/json' \
--header "Authorization: Bearer $TOKEN" \
--data "{
  \"comment\": \"Updated $(TZ='America/New_York' date)\",
  \"name\": \"wg.carter2099.com\",
  \"proxied\": false,
  \"settings\": {},
  \"tags\": [],
  \"ttl\": 1,
  \"content\": \"$(curl ifconfig.me)\",
  \"type\": \"A\"
}"
