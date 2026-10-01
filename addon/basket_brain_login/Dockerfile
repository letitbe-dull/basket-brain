ARG BUILD_FROM="ghcr.io/home-assistant/amd64-base-debian:bookworm"
FROM ${BUILD_FROM}

LABEL \
    io.hass.name="Basket Brain Login" \
    io.hass.description="Stealth browser login service for Woolworths and Foodstuffs NZ." \
    io.hass.type="addon" \
    io.hass.arch="aarch64|amd64"

# System libraries required by Firefox (camoufox) headless on Debian/glibc.
# Alpine/musl is explicitly NOT used — Firefox won't run on musl libc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    # Firefox runtime deps
    libgtk-3-0 \
    libdbus-glib-1-2 \
    libxt6 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libxss1 \
    # X client libs Firefox links against even when headless
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcb-shm0 \
    libxcursor1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrender1 \
    libxtst6 \
    # Software GL/WebGL (no GPU in the container)
    libgl1 \
    libegl1 \
    # Fonts — pages render blank/odd without at least one font
    fontconfig \
    fonts-dejavu-core \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Use a venv — Debian Bookworm enforces PEP 668 on the system Python.
RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"

# camoufox — Firefox-based anti-detect browser (beats Akamai bot detection).
# playwright is pinned: camoufox's patched Firefox speaks the ~1.49 protocol;
# newer playwright sends fields (e.g. viewport.isMobile) the browser rejects.
# aiohttp — async HTTP server for the login API (Phase 2).
RUN pip install --no-cache-dir \
    "camoufox==0.5.6" \
    "playwright==1.49.1" \
    "aiohttp>=3.9"

# Pre-download the Firefox binary for the build platform at image build time
# so the container is fully self-contained and starts offline.
RUN python3 -m camoufox fetch

COPY run.sh /run.sh
RUN chmod a+x /run.sh

COPY server.py /app/server.py
COPY login_woolworths.py /app/login_woolworths.py
COPY me_check.py /app/me_check.py
COPY login_foodstuffs.py /app/login_foodstuffs.py

CMD ["/run.sh"]
