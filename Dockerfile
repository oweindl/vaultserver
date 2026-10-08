# VaultServer als Container. Konfiguration, Vault und Daten kommen per Volume
# von außen (siehe compose.yaml), das Image enthält nur Code und git.
FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Der Container läuft mit der UID des Host-Benutzers (compose: user:), damit
# Dateien und git-Objekte im Vault ihm gehören. git prüft den Besitzer des
# Repos, das HOME gibt es für diese UID nicht – daher systemweite Einstellungen.
# Zugang zu GitHub: HTTPS mit Token aus GIT_TOKEN (nur wenn gesetzt).
RUN git config --system safe.directory '*' \
 && git config --system credential.helper \
    '!f() { test -n "$GIT_TOKEN" || exit 0; echo username=x-access-token; echo "password=$GIT_TOKEN"; }; f'

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 HOME=/tmp
EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/healthz', timeout=4)"

ENTRYPOINT ["vaultserver", "-c", "/config/vaultserver.toml"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8100"]
