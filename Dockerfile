# Use the official Playwright image as the base image
FROM mcr.microsoft.com/playwright/python:v1.63.0-jammy

# Tell Poetry where to place its cache and virtual environment
ENV POETRY_HOME=/opt/poetry \
    POETRY_VENV=/opt/poetry-venv \
    POETRY_DATA_DIR=/opt/poetry-data \
    POETRY_CONFIG_DIR=/opt/poetry-config \
    POETRY_CACHE_DIR=/opt/.cache \
    PYTHONUNBUFFERED=1 \
    BROWSER_MODE=headful \
    DISPLAY=:99 \
    APP_HOME=/app \
    AUTH_TOKEN=

RUN useradd -m -u 1001 appuser

WORKDIR ${APP_HOME}

RUN curl -sSL https://install.python-poetry.org | python3 -
ENV PATH="${PATH}:${POETRY_HOME}/bin"

COPY pyproject.toml poetry.lock README.md ./
# install only deps in dependency list first and lockfile to cache them
RUN poetry config virtualenvs.create false && poetry install --no-root --only main

# Install a chromedriver build matching the bundled Chromium major version
RUN apt-get update \
    && apt-get install -y curl jq unzip \
    && rm -rf /var/lib/apt/lists/* \
    && CHROME_BIN=$(ls -d /ms-playwright/chromium-*/chrome-linux*/chrome | head -n1) \
    && CHROME_MAJOR=$("$CHROME_BIN" --version | sed -E 's/.* ([0-9]+)\..*/\1/') \
    && case "$(dpkg --print-architecture)" in \
         amd64) CFT_PLATFORM=linux64 ;; \
         arm64) CFT_PLATFORM=linux-arm64 ;; \
         *) echo "Unsupported architecture: $(dpkg --print-architecture)" && exit 1 ;; \
       esac \
    && DRIVER_URL=$(curl -s https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json \
         | jq -r --arg major "$CHROME_MAJOR" --arg platform "$CFT_PLATFORM" \
           '[.versions[] | select(.version | startswith($major + "."))] | sort_by(.version | split(".") | map(tonumber)) | last | .downloads.chromedriver[] | select(.platform == $platform) | .url') \
    && test -n "$DRIVER_URL" \
    && curl -sL "$DRIVER_URL" -o /tmp/chromedriver.zip \
    && unzip /tmp/chromedriver.zip -d /tmp \
    && mv /tmp/chromedriver-*/chromedriver /usr/local/bin/chromedriver \
    && chmod +x /usr/local/bin/chromedriver \
    && rm -rf /tmp/chromedriver.zip /tmp/chromedriver-*

COPY src ./src

# Install server module
RUN poetry install --compile --no-interaction --no-ansi --only main

# Remove Poetry
RUN curl -sSL https://install.python-poetry.org | python3 - --uninstall

# Ensure necessary directories exist and have correct permissions
RUN mkdir -p /opt/poetry-data /opt/.cache /opt/poetry-config /opt/poetry-venv \
    && chown -R appuser:appuser /opt/poetry-data /opt/.cache /opt/poetry-config /opt/poetry-venv ${APP_HOME}

# Switch to non-root user
USER appuser

EXPOSE 8000

CMD ["sh", "-c", "Xvfb :99 -screen 0 1024x768x16 & start"]
