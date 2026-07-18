#!/usr/bin/with-contenv bashio
# Entrypoint for the Basket Brain Login add-on.

if bashio::config.is_empty 'api_token'; then
    bashio::log.warning "No api_token set — /login stays closed until the integration configures one."
    export API_TOKEN=""
else
    export API_TOKEN="$(bashio::config 'api_token')"
fi

bashio::log.info "Basket Brain Login starting on port 8099..."
exec /venv/bin/python3 /app/server.py
