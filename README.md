# Google Ads MCP Server

This repo contains the source code for running an
[MCP](https://modelcontextprotocol.io) server that interacts with the
[Google Ads API](https://developers.google.com/google-ads/api).

## Tools

The server uses the
[Google Ads API](https://developers.google.com/google-ads/api/reference/rpc/latest/overview)
to provide several
[Tools](https://modelcontextprotocol.io/docs/concepts/tools) and [Resources](https://modelcontextprotocol.io/docs/concepts/tools) for use with LLMs and AI agents.

### Tools available

- `search`: Retrieves information about the Google Ads account.
- `get_resource_metadata`: Retrieves metadata about a Google Ads API resource type, for example "campaign". This is useful to understand the structure of the data and what fields are available for querying.
- `list_accessible_customers`: Returns ids of customers directly accessible
  by the user authenticating the call.
- `list_accounts`: Lists accessible accounts, including client accounts of
  manager accounts, with their name, currency and time zone.

#### Managing campaigns

The following tools create and change entities in your Google Ads accounts.
New campaigns are always created **paused**: nothing spends money until a
campaign is enabled with `enable_campaign`. The server tells agents to read
the current state before changing it, and enforces configurable
[spend guardrails](#spend-guardrails). Every mutating tool accepts
`validate_only: true` to check a request without changing anything, and
monetary amounts are given in the account currency (e.g. `25.5`), not micros.

- `campaigns` namespace:
  - `list_campaigns`: Campaigns with status, bidding, daily budget and
    performance over a date range.
  - `get_campaign`: Status, budget, bidding, targeting, asset/ad groups and
    last-30-day performance of a campaign.
  - `get_spend_overview`: Total daily budget of enabled campaigns, spend today
    and this month, billing spending limits, and guardrail headroom.
  - `create_search_campaign`: Search campaign with budget, targeting, an ad
    group, keywords and a responsive search ad, for websites and web apps.
  - `create_pmax_campaign`: Performance Max campaign with its first asset group.
  - `create_app_campaign`: App campaign promoting an Android or iOS app,
    optimizing for installs, in-app actions or in-app value.
  - `update_budget`, `pause_campaign`, `enable_campaign`.
- `assets` namespace:
  - `list_assets`, `get_asset_group`: Existing assets, and the assets and
    signals of a Performance Max asset group.
  - `upload_image`, `upload_logo`: Upload images from a URL, base64 data or a
    local file, and report which image formats they fit.
  - `create_text_assets`, `create_youtube_video_assets`.
  - `create_asset_group`: Add an asset group to a Performance Max campaign.
- `targeting` namespace:
  - `find_geo_targets`: Look up location IDs by name.
  - `list_audiences`: Audiences usable as Performance Max signals.
  - `set_geo_targets`, `set_language_targets`, `set_negative_keywords`,
    `set_audience_signals`: Add, remove or replace targeting.
- `conversions` namespace:
  - `list_conversion_actions`, `get_conversion_action`,
    `create_conversion_action`,
    `update_conversion_action`: Manage what counts as a conversion. Website
    conversion actions return the tag snippets to install.
  - `upload_click_conversions`: Report conversions recorded by your backend.

Mobile app conversions (installs, in-app events) are imported by linking
Firebase, Google Analytics 4, Google Play or a third-party app analytics
provider in the Google Ads UI; they then appear in `list_conversion_actions`.

To run a read-only server, disable the `campaigns`, `assets`, `targeting` and
`conversions` namespaces in `tools_config.yaml` (see below).

### Configuring and Namespacing Tools

The Google Ads MCP server uses the `tools_config.yaml` to let you selectively enable or disable individual tools or tool categories (namespaces) and customize their namespace prefixes.

A default `tools_config.yaml` with all tools enabled is bundled with the package, so the server works out of the box with no extra setup. To customize your installation, the server resolves the configuration in the following order:

1. An explicit path set via the `GOOGLE_ADS_MCP_TOOLS_CONFIG` environment variable.
2. A `tools_config.yaml` file in the current working directory.
3. The default `tools_config.yaml` bundled with the package.

If an explicitly requested configuration file (via the environment variable) is missing, or any resolved file is invalid, the server raises an error and fails to start.

#### Configuration Example:
```yaml
namespaces:
  # Option 1: Enable category 'customers' with default prefix -> "customers_list_accessible_customers"
  customers: true

  # Option 2: Enable category 'search' with a custom prefix -> "query_search"
  search: "query"

  # Option 3: Fine-grained control over tools in a category
  metadata:
    enabled: true
    prefix: "metadata"
    enabled_tools:
      - get_resource_metadata: true
```


### Agent skill

[`skills/google-ads`](skills/google-ads/SKILL.md) is an agent skill that
teaches Claude how to use these tools well: read the account before changing
it, pick the right campaign type for web vs. mobile apps, set up conversion
tracking, meet creative requirements and respect spend guardrails. See
[Install locally with Claude Code](#install-locally-with-claude-code) to set it
up alongside the server.

### Spend guardrails

Set limits in the `guardrails` section of `tools_config.yaml`, in each
account's currency. They are checked against live account data before
`create_*_campaign`, `update_budget` and `enable_campaign` send any change, and
cannot be overridden through tool parameters. Pausing and lowering budgets is
never blocked.

```yaml
guardrails:
  max_daily_budget: 100             # per campaign
  max_total_daily_budget: 500       # all enabled campaigns in an account
  max_budget_increase_percent: 50   # per update_budget call
  accounts:                         # per-account overrides
    "1234567890":
      max_total_daily_budget: 2000
```

The configuration is re-read on every call, so edits apply without a restart.

### Resources available

- `discovery-document`: Retrieve the Google Ads API discovery document. Provides the discovery document for the latest version of the Google Ads API, which describes the API surface, including resources, methods, and schemas. Host LLMs should access this resource to understand the structure of the Google Ads API and discover available features.
- `metrics`: Retrieve information about the metrics available for reporting in the Google Ads API.
- `segments`: Retrieve information about the segments available for reporting in the Google Ads API.
- `release-notes`: Retrieve the release notes for the latest version of the Google Ads API.

## Notes

1.  The MCP Server will expose your data to the Agent or LLM that you connect to it.
1.  Unless you disable them, the MCP Server lets the Agent or LLM create and
    change campaigns, which can spend money once enabled.
1.  If you have technical issues, please use the [GitHub issue tracker](https://github.com/googleads/google-ads-mcp/issues).
1.  To help us collect usage data, you will notice an extra header has been added to your API calls: this data is used to improve the product.

## Setup instructions

Setup involves the following steps:

1.  Configure Python.
1.  Configure Developer Token.
1.  Enable APIs in your project
1.  Configure Credentials.
1.  Configure your MCP client.

To run the server from a local checkout of this repository (for example to
use tools that are not yet released) and install the agent skill, do the
first four steps, then follow
[Install locally with Claude Code](#install-locally-with-claude-code).

### Configure Python

[Install pipx](https://pipx.pypa.io/stable/#install-pipx).

After a version has been published to PyPI, you can run that exact version
instead of following the latest repository state:

```shell
pipx run --spec "google-ads-mcp==X.Y.Z" google-ads-mcp
```

### Configure Developer Token (Optional)

If your setup requires a developer token, follow the instructions for [Obtaining a Developer Token](https://developers.google.com/google-ads/api/docs/get-started/dev-token).

Your developer token must have at least [Explorer access](https://developers.google.com/google-ads/api/docs/get-started/dev-token#access-levels) to query production accounts. New tokens may be automatically upgraded to Explorer access; if not, you can apply through the API Center. See the [access levels documentation](https://developers.google.com/google-ads/api/docs/get-started/dev-token#access-levels) for details.

If you see the error *"The developer token is only approved for use with test
accounts"*, your token does not yet have access to production accounts. See the
[access levels documentation](https://developers.google.com/google-ads/api/docs/access-levels)
for how to request the access level you need.

### Enable APIs in your project

[Follow the instructions](https://support.google.com/googleapi/answer/6158841)
to enable the following APIs in your Google Cloud project:

* [Google Ads API](https://console.cloud.google.com/apis/library/googleads.googleapis.com)

### Configure Credentials
#### Option 1: Using FastMCP OAuth Proxy

The server supports FastMCP's [OAuth proxy](https://gofastmcp.com/servers/auth/oauth-proxy) feature for dynamic user authentication. This is useful when running the server as a web service.

To enable it, set the following environment variables:

- `GOOGLE_ADS_MCP_OAUTH_CLIENT_ID`: Your Google Cloud OAuth 2.0 Client ID.
- `GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET`: Your Google Cloud OAuth 2.0 Client Secret.
- `GOOGLE_ADS_MCP_BASE_URL`: (Optional) The base URL where the server is accessible (defaults to `http://localhost:8080`).
- `GOOGLE_ADS_MCP_JWT_SIGNING_KEY`: (Optional) Secret key used to sign FastMCP JWT tokens across multiple server instances or deployments.
- `GOOGLE_ADS_MCP_STORAGE_TYPE`: (Optional) Storage backend for OAuth state (`filetree`, `redis`, `firestore`, or `memory`).
- `GOOGLE_ADS_MCP_STORAGE_PATH`: (Optional) Directory path for `filetree` persistent storage.
- `GOOGLE_ADS_MCP_STORAGE_REDIS_URL`: (Optional) Redis URL for `redis` persistent storage.
- `GOOGLE_ADS_MCP_STORAGE_FIRESTORE_PROJECT`: (Optional) Google Cloud project for `firestore` persistent storage. Defaults to the project inferred from Application Default Credentials. Setting it selects the `firestore` backend even if `GOOGLE_ADS_MCP_STORAGE_TYPE` is unset.
- `GOOGLE_ADS_MCP_STORAGE_FIRESTORE_DATABASE`: (Optional) Firestore database name for `firestore` persistent storage. Defaults to `(default)`.
- `GOOGLE_ADS_MCP_STORAGE_ENCRYPTION_KEY`: (Optional) Encryption key for stored OAuth tokens.
- `GOOGLE_ADS_MCP_STORAGE_DISABLE_ENCRYPTION`: (Optional) Set to `true` to disable token encryption.

The `redis` and `firestore` backends need their storage library installed
alongside the server: `pip install py-key-value-aio[redis]` and
`pip install google-ads-mcp[firestore]` respectively.

Once this is enabled, you can authenticate to the API through your MCP client.

When these variables are set, the server automatically switches to the
`streamable-http` transport instead of `stdio`.

You will need to run the server as a separate process and configure your MCP
client to connect to the Streamable HTTP endpoint (for example,
`http://localhost:8080/mcp`).

### Local WSL and Podman deployment

This deployment has been tested with rootless Podman in WSL and exposes a single
Streamable HTTP endpoint at `http://localhost:8080/mcp`. Build the image from
the repository inside WSL:

```shell
podman build --tag localhost/google-ads-mcp:latest --file Dockerfile .
```

Keep server credentials out of MCP client configuration. The tested Quadlet
loads Google Ads and OAuth settings from a private host-side file through
`EnvironmentFile=` and uses a separate named volume for persistent encrypted
OAuth state:

```ini
[Container]
Image=localhost/google-ads-mcp:latest
PublishPort=127.0.0.1:8080:8080
EnvironmentFile=/absolute/host/path/google-ads-mcp.env
Volume=google-ads-mcp-oauth.volume:/var/lib/google-ads-mcp:rw
ReadOnly=true
NoNewPrivileges=true
DropCapability=all
```

The environment file and the OAuth-state volume serve different purposes: the
volume does not contain the `.env` file. Keep the environment file outside the
repository, restrict it to the service owner, and never commit it. Antigravity
and Codex then need only the MCP endpoint and their own OAuth authorization;
they do not need the server's Google Ads developer token, OAuth client secret,
or signing and storage keys. Publish port 8080 only on the loopback interface
when the server is intended for local agents.

The endpoint deliberately keeps stateful Streamable HTTP enabled. It supports
legacy MCP 2025 clients that use `Mcp-Session-Id` and GET SSE as well as MCP
2026 clients that use sessionless POST requests and `subscriptions/listen`.
Do not enable FastMCP's `stateless_http` option on this shared endpoint; doing
so removes the legacy GET channel.

The server runs on FastMCP 4 (`fastmcp>=4.0.3`) paired with `mcp[cli]==2.0.0`. The Docker build also applies a version-guarded
OAuth metadata workaround for Codex CLI 0.146. It stops advertising the RFC
9207 authorization-response `iss` parameter as mandatory while FastMCP still
includes it in redirects. The build fails if the expected FastMCP version or
patch location changes, so upgrades require explicit interoperability tests.

For Codex, configure and authenticate the server as described in the
[official Codex MCP documentation](https://developers.openai.com/codex/mcp/):

```shell
codex mcp add google_ads --url http://localhost:8080/mcp
codex mcp login google_ads
```

For Antigravity, configure the same URL as `serverUrl` in its MCP configuration.
This key is required for Streamable HTTP in Antigravity 2.8.1 and Antigravity
IDE 2.5.5; `httpUrl` is not accepted by those versions. After authentication,
both clients should list these namespaced tools:

- `customers_list_accessible_customers`
- `metadata_get_resource_metadata`
- `search_search`

#### Option 2: Configure credentials using Application Default Credentials

Configure your [Application Default Credentials
(ADC)](https://cloud.google.com/docs/authentication/provide-credentials-adc).
Make sure the credentials are for a user with access to your Google Ads
accounts or properties.

Credentials must include the Google Ads API scope:

```
https://www.googleapis.com/auth/adwords
```

Check out
[Manage OAuth Clients](https://support.google.com/cloud/answer/15549257)
for how to create an OAuth client.

Here are some sample `gcloud` commands you might find useful:


- Set up ADC using user credentials and an OAuth desktop or web client after
  downloading the client JSON to `YOUR_CLIENT_JSON_FILE`.

  ```shell
  gcloud auth application-default login \
    --scopes https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform \
    --client-id-file=YOUR_CLIENT_JSON_FILE
  ```

- Set up ADC using service account impersonation.

  ```shell
  gcloud auth application-default login \
    --impersonate-service-account=SERVICE_ACCOUNT_EMAIL \
    --scopes=https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform
  ```

When the `gcloud auth application-default` command completes, copy the
`PATH_TO_CREDENTIALS_JSON` file location printed to the console in the
following message. You will need this for a later step!

```
Credentials saved to file: [PATH_TO_CREDENTIALS_JSON]
```

#### Option 3: Configure credentials using the Google Ads API Python client library.

[Follow the instructions](https://developers.google.com/google-ads/api/docs/client-libs/python/)
to setup and configure the Google Ads API Python client library

If you have already done this and have a working `google-ads.yaml` , you can reuse this file!

In the utils.py file, change get_googleads_client() to use the load_from_storage() method.

### Configure your MCP client

Add the server to your MCP client's configuration. Below are examples for
popular clients.

#### Antigravity / Antigravity IDE

1.  Install [Antigravity](https://antigravity.google/product/antigravity-cli)
    or Antigravity IDE.

1.  Configure your server. Refer to the docs at [https://antigravity.google/docs/mcp](https://antigravity.google/docs/mcp) for details on setting up MCP servers.

- Option 1: Using FastMCP OAuth Proxy (Streamable HTTP)

  You can run the server as a separate process and configure your MCP client
  to connect to the Streamable HTTP endpoint (for example,
  `http://localhost:8080/mcp`).
  This also allows using FastMCP's [OAuth proxy](https://gofastmcp.com/servers/auth/oauth-proxy) feature for dynamic user authentication.

  Antigravity 2.8.1 and Antigravity IDE 2.5.5 require `serverUrl` for a
  Streamable HTTP server. Do not use the older `httpUrl` key. Server-side
  credentials belong in the server process, not in this client configuration.

    ```json
    {
      "mcpServers": {
        "google-ads-mcp": {
          "serverUrl": "http://localhost:8080/mcp"
        }
      }
    }
    ```

- Option 2: the Application Default Credentials method

    This remains a supported alternative, but it provides less credential
    isolation than the server-managed Streamable HTTP deployment above. The MCP
    client starts the server and its configuration contains the ADC file path
    and Google Ads developer token. Prefer the Quadlet deployment when several
    local clients share the same server or client configuration may be copied,
    synchronized, or inspected by other tools.

    Replace `PATH_TO_CREDENTIALS_JSON` with the path you copied in the previous
    step.

    We also recommend that you add a `GOOGLE_CLOUD_PROJECT` attribute to the
    `env` object. Replace `YOUR_PROJECT_ID` in the following example with the
    [project ID](https://support.google.com/googleapi/answer/7014113) of your
    Google Cloud project.

    ```json
    {
      "mcpServers": {
        "google-ads-mcp": {
          "command": "pipx",
          "args": [
            "run",
            "--spec",
            "git+https://github.com/googleads/google-ads-mcp.git",
            "google-ads-mcp"
          ],
          "env": {
            "GOOGLE_APPLICATION_CREDENTIALS": "PATH_TO_CREDENTIALS_JSON",
            "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
            "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN"
          }
        }
      }
    }
    ```

- Option 3: the Python client library method

    ```json
    {
      "mcpServers": {
        "google-ads-mcp": {
          "command": "pipx",
          "args": [
            "run",
            "--spec",
            "git+https://github.com/googleads/google-ads-mcp.git",
            "google-ads-mcp"
          ],
          "env": {
            "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
            "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN"
          }
        }
      }
    }
    ```

#### Login Customer Id

If your access to the customer account is through a manager account, you can
either provide the manager account's customer ID per tool call via the optional
`login_customer_id` parameter, or set `GOOGLE_ADS_LOGIN_CUSTOMER_ID` in the
settings file as a default (the per-call `login_customer_id` parameter takes
precedence when specified).

See [here](https://developers.google.com/google-ads/api/docs/concepts/call-structure#cid) for details.

The final file will look like this:

  ```json
  {
    "mcpServers": {
      "google-ads-mcp": {
        "command": "pipx",
        "args": [
          "run",
          "--spec",
          "git+https://github.com/googleads/google-ads-mcp.git",
          "google-ads-mcp"
        ],
        "env": {
          "GOOGLE_APPLICATION_CREDENTIALS": "PATH_TO_CREDENTIALS_JSON",
          "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
          "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN",
          "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "YOUR_MANAGER_CUSTOMER_ID"
        }
      }
    }
  }
  ```

#### Install locally with Claude Code

These steps run the MCP server from a local checkout of this repository and
install the [agent skill](skills/google-ads/SKILL.md), so Claude Code can
use them in any project. They assume you have completed the credential steps
above (Application Default Credentials or a `google-ads.yaml` file) and have a
developer token.

1.  **Install [uv](https://docs.astral.sh/uv/getting-started/installation/)
    and clone the repository.** Use an absolute path you will keep; the
    commands below refer to it as `$GOOGLE_ADS_MCP_DIR`.

    ```shell
    git clone https://github.com/googleads/google-ads-mcp.git ~/src/google-ads-mcp
    export GOOGLE_ADS_MCP_DIR=~/src/google-ads-mcp
    cd "$GOOGLE_ADS_MCP_DIR" && uv sync
    ```

    `uv sync` creates a `.venv` in the checkout with the server installed in
    editable mode, so local code changes apply the next time the server starts.

1.  **Create your own tools configuration** outside the checkout, so that
    `git pull` never overwrites it, and set your
    [spend guardrails](#spend-guardrails):

    ```shell
    mkdir -p ~/.config/google-ads-mcp
    cp "$GOOGLE_ADS_MCP_DIR/ads_mcp/tools_config.yaml" ~/.config/google-ads-mcp/
    # Edit ~/.config/google-ads-mcp/tools_config.yaml: set the guardrails, or
    # disable the campaigns/assets/targeting/conversions namespaces for a
    # read-only server.
    ```

1.  **Register the MCP server with Claude Code.** To make it available in all
    your projects (user scope):

    ```shell
    claude mcp add google-ads --scope user \
      -e GOOGLE_APPLICATION_CREDENTIALS=PATH_TO_CREDENTIALS_JSON \
      -e GOOGLE_PROJECT_ID=YOUR_PROJECT_ID \
      -e GOOGLE_ADS_DEVELOPER_TOKEN=YOUR_DEVELOPER_TOKEN \
      -e GOOGLE_ADS_MCP_TOOLS_CONFIG="$HOME/.config/google-ads-mcp/tools_config.yaml" \
      -- uv --directory "$GOOGLE_ADS_MCP_DIR" run google-ads-mcp
    ```

    Add `-e GOOGLE_ADS_LOGIN_CUSTOMER_ID=YOUR_MANAGER_CUSTOMER_ID` if you
    access accounts through a manager account. Omit
    `GOOGLE_APPLICATION_CREDENTIALS` if you use `gcloud auth
    application-default login`, which stores credentials in the default
    location.

    To share the setup with a team through a project's repository instead, add
    a `.mcp.json` file at the project root. Claude Code expands `${VAR}` from
    each user's environment, so no secrets are committed:

    ```json
    {
      "mcpServers": {
        "google-ads": {
          "command": "uv",
          "args": [
            "--directory",
            "${GOOGLE_ADS_MCP_DIR}",
            "run",
            "google-ads-mcp"
          ],
          "env": {
            "GOOGLE_APPLICATION_CREDENTIALS": "${GOOGLE_APPLICATION_CREDENTIALS}",
            "GOOGLE_PROJECT_ID": "${GOOGLE_PROJECT_ID}",
            "GOOGLE_ADS_DEVELOPER_TOKEN": "${GOOGLE_ADS_DEVELOPER_TOKEN}",
            "GOOGLE_ADS_MCP_TOOLS_CONFIG": "${GOOGLE_ADS_MCP_TOOLS_CONFIG}"
          }
        }
      }
    }
    ```

1.  **Install the agent skill.** For all projects, link it into your user
    skills directory; for one project, link it into that project's
    `.claude/skills` directory instead (commit a copy rather than a link if
    teammates should get it too):

    ```shell
    # All projects
    mkdir -p ~/.claude/skills
    ln -s "$GOOGLE_ADS_MCP_DIR/skills/google-ads" ~/.claude/skills/google-ads

    # One project (run from the project root)
    mkdir -p .claude/skills
    cp -r "$GOOGLE_ADS_MCP_DIR/skills/google-ads" .claude/skills/
    ```

    A link picks up skill updates with `git pull`; a copy has to be refreshed.

1.  **Check the setup.** Run `claude mcp list` and confirm `google-ads` is
    connected, then start Claude Code: `/mcp` lists the server and its tools,
    and `/google-ads` invokes the skill directly (it is also used
    automatically for Google Ads requests). Try a read-only request first,
    such as *"List my Google Ads accounts and show the spend overview for the
    main one."*

To update, run `git pull` (and `uv sync` if dependencies changed) in the
checkout, then restart Claude Code.

#### Other MCP clients (Cursor, VS Code, etc.)

The `mcpServers` block format is the same across all MCP clients. Add the configuration shown above to the appropriate settings file for your client (e.g., `.mcp.json` for Claude Code, `.cursor/mcp.json` for Cursor, `.vscode/mcp.json` for VS Code with Copilot).

To run a local checkout instead of the published version, replace `command`
and `args` with `"command": "uv"` and
`"args": ["--directory", "/absolute/path/to/google-ads-mcp", "run", "google-ads-mcp"]`,
and set `GOOGLE_ADS_MCP_TOOLS_CONFIG` in `env` to your tools configuration.
The agent skill is specific to Claude Code, but `skills/google-ads/SKILL.md`
is plain Markdown that other agents can be pointed to as instructions.

## Deployment to Google Cloud Platform

Instead of hosting this MCP server locally, you can host it on Google Cloud Run or on any other cloud-based infrastructure. This is useful if you want to share the server across different agents or run it as a web service.

Note that this only supports authentication with an OAuth Client ID and Client Secret pair through the OAuth proxy (Option #1 above).

### Prerequisites

1.  A Google Cloud project.
2.  The `gcloud` CLI installed, authenticated, and active project set.
    ```shell
    gcloud config set project YOUR_PROJECT_ID
    ```

### Step 1: Build and Push Docker Image

You can use Cloud Build to build and push the image to Artifact Registry without needing Docker installed locally.

1.  Create a repository in Artifact Registry:
    ```shell
    gcloud artifacts repositories create mcp-servers --repository-format=docker --location=us-central1
    ```
2.  Build and submit the image:
    ```shell
    gcloud builds submit --tag us-central1-docker.pkg.dev/YOUR_PROJECT_ID/mcp-servers/google-ads-mcp:latest .
    ```
    Replace `YOUR_PROJECT_ID` with your Google Cloud project ID.

### Step 2: Deploy to Google Cloud Run

Make sure to set the required environment variables:

- `GOOGLE_PROJECT_ID`: Your Google Cloud project ID.
- `GOOGLE_ADS_DEVELOPER_TOKEN`: (Optional) The developer token you want the MCP server to use (see above).
- `GOOGLE_ADS_MCP_OAUTH_CLIENT_ID`: The OAuth Client ID you want the MCP server to use.
- `GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET`: The OAuth Client secret you want the MCP server to use.
- `GOOGLE_ADS_MCP_BASE_URL`: The base URL where your MCP server is accessible: this will be automatically assigned by Google Cloud Run after your first deployment. You can update the environment variables after deployment. 
- `GOOGLE_ADS_MCP_JWT_SIGNING_KEY`: (Recommended for production) Persistent JWT signing key across Cloud Run instances.
- `GOOGLE_ADS_MCP_STORAGE_TYPE`: (Recommended for production) Storage backend to persist OAuth tokens across instances. Set it to `firestore` to use Firestore through Application Default Credentials, which needs no VPC connector, or to `redis` along with `GOOGLE_ADS_MCP_STORAGE_REDIS_URL`.

  Using `firestore` requires three things: build the image with the extra
  installed (change the Dockerfile to `uv pip install --system .[firestore]`),
  create a Firestore database in the project, since one is not provisioned
  automatically, and grant the Cloud Run service account `roles/datastore.user`.
  Note that entries are not expired automatically: the store filters expired
  entries on read but never deletes them, and `expires_at` is written as a
  string, so a Firestore TTL policy cannot collect them either. Plan on a
  periodic cleanup job for long-running deployments. Redis expires entries on
  its own.
- `FASTMCP_HOST`: Set this to `0.0.0.0` to allow FastMCP to accept connections from all IP addresses.
- `GOOGLE_ADS_LOGIN_CUSTOMER_ID`: Required if your access to the customer account is through a manager account. Set it to the customer ID of the manager account. See [Login Customer Id](#login-customer-id) above for details.

```shell
gcloud run deploy google-ads-mcp \
  --image us-central1-docker.pkg.dev/YOUR_PROJECT_ID/mcp-servers/google-ads-mcp:latest \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars="GOOGLE_PROJECT_ID=YOUR_PROJECT_ID,GOOGLE_ADS_DEVELOPER_TOKEN=YOUR_DEVELOPER_TOKEN,GOOGLE_ADS_MCP_OAUTH_CLIENT_ID=YOUR_CLIENT_ID,GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET=YOUR_CLIENT_SECRET,GOOGLE_ADS_MCP_BASE_URL=YOUR_BASE_URL,GOOGLE_ADS_MCP_JWT_SIGNING_KEY=YOUR_JWT_SIGNING_KEY,GOOGLE_ADS_MCP_STORAGE_TYPE=firestore,FASTMCP_HOST=0.0.0.0"
```

### Step 3: Configure MCP Client

Once deployed, update your MCP client configuration (refer to the docs at [https://antigravity.google/docs/mcp](https://antigravity.google/docs/mcp)) to use the Cloud Run URL.

```json
{
  "mcpServers": {
    "google-ads-mcp": {
      "httpUrl": "https://your-cloud-run-url.a.run.app/mcp"
    }
  }
}
```

## Try it out

Launch your MCP client. You should see `google-ads-mcp` listed in the
available servers.

Here are some sample prompts to get you started:

- Ask what the server can do:

  ```
  what can the ads-mcp server do?
  ```

- Ask about customers:

  ```
  what customers do I have access to?
  ```

- Ask about campaigns 

  ```
  How many active campaigns do I have?
  ```

  ```
  How is my campaign performance this week?
  ```

### Note about Customer ID

Your agent will need and ask for a customer id for most prompts. If you are 
moving between multiple customers, including the customer ID in the prompt may
be simpler.

```
How many active campaigns do I have for customer id 1234567890
```

## Contributing

Contributions welcome! See the [Contributing Guide](CONTRIBUTING.md).
Project maintainers can find the Trusted Publishing and release procedure in
the [release guide](docs/releasing.md).
