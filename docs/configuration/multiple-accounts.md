# Multiple accounts

One archive can hold several Telegram accounts.

## Single-account and indexed mode

An install starts in single-account mode. It reads one account from `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` and `TELEGRAM_PHONE`.

Indexed mode declares accounts with numbered variables:

| Variable | Required | Meaning |
|---|---|---|
| `TG_ACCOUNT_<N>_API_ID` | yes | Integer API id from my.telegram.org |
| `TG_ACCOUNT_<N>_API_HASH` | yes | API hash |
| `TG_ACCOUNT_<N>_PHONE_NUMBER` | yes | Phone number with country code |
| `TG_ACCOUNT_<N>_LABEL` | no | Name shown in the viewer |
| `TG_ACCOUNT_<N>_SESSION_NAME` | no | Session file name in `SESSION_DIR` |

Any non-empty value in one of these five variables switches the install to indexed mode. From then on, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` and `TELEGRAM_PHONE` are ignored. A per-account filter override on its own never switches modes, so a single-account install can still use `TG_ACCOUNT_1_` filters.

## Rules for account variables

- `N` starts at 1 and has no leading zeros.
- Indexes are contiguous: account 3 needs accounts 1 and 2.
- Each account needs `API_ID`, `API_HASH` and `PHONE_NUMBER`. `API_ID` must be an integer.
- Phone numbers must differ between accounts.
- Session names must differ between accounts.
- A `TG_ACCOUNT_` variable with an unknown suffix stops startup.
- An empty value counts as unset.
- Errors about the credential variables name the variable, never its value.

## Labels and session names

The label defaults to `default` for account 1 and `account<N>` for the others. The label is read again on every start, so you can rename an account at any time.

Session names resolve like this:

| Account | Session name, first match wins |
|---|---|
| 1 | `TG_ACCOUNT_1_SESSION_NAME`, then `SESSION_NAME`, then `telegram_backup` |
| 2 and up | `TG_ACCOUNT_<N>_SESSION_NAME`, then `telegram_backup_account<N>` |

Account 1 follows the same chain as single-account mode. That is what lets an existing install add a second account without logging in again.

## Going from one account to two

1. Stop the stack.

    ```bash
    docker compose down
    ```

2. In `.env`, move the three `TELEGRAM_*` values to `TG_ACCOUNT_1_*`. Leave account 1's session name as it was: do not set `TG_ACCOUNT_1_SESSION_NAME`, and keep `SESSION_NAME` if you had set it. The existing session file is then reused.

    Compose then warns that `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` and `TELEGRAM_PHONE` are not set. The warnings are harmless: in indexed mode those variables are ignored. To silence them, leave the three lines in `.env` with the old values.

3. Add the `TG_ACCOUNT_2_*` variables.

    ```dotenv
    # Account 1: the credentials that used to be TELEGRAM_*
    TG_ACCOUNT_1_API_ID=12345678
    TG_ACCOUNT_1_API_HASH=0123456789abcdef0123456789abcdef
    TG_ACCOUNT_1_PHONE_NUMBER=+15550100001
    TG_ACCOUNT_1_LABEL=Personal

    # Account 2: the new one
    TG_ACCOUNT_2_API_ID=87654321
    TG_ACCOUNT_2_API_HASH=fedcba9876543210fedcba9876543210
    TG_ACCOUNT_2_PHONE_NUMBER=+15550100002
    TG_ACCOUNT_2_LABEL=Work
    ```

4. Log in. The login checks every account and skips any account that is already logged in. It asks only for the new account's code, then its two-step password if the account has one. It fails if the account that logs in does not own the configured phone number.

    ```bash
    docker compose run --rm telegram-backup python -m src auth
    ```

    The 8.16.1 images know the module only by its old name, `src`. Later images keep `src` as an alias, so this command works on both.

5. Start the stack.

    ```bash
    docker compose up -d
    ```

The next run fetches the new account's history from the beginning. Account 1 continues from where it stopped.

The stock `docker-compose.yml` loads `.env` into the backup service through `env_file`, so the `TG_ACCOUNT_*` variables reach it with no extra step. The viewer does not need them.

See [Log in to Telegram](../getting-started/telegram-login.md) for the login itself.

## Per-account filters

Each chat filter can be set for one account by adding the `TG_ACCOUNT_<N>_` prefix. These suffixes are accepted:

| Suffix | Overrides |
|---|---|
| `CHAT_IDS` | `CHAT_IDS` |
| `CHAT_TYPES` | `CHAT_TYPES` |
| `INCLUDE_CHAT_IDS` | `GLOBAL_INCLUDE_CHAT_IDS` |
| `EXCLUDE_CHAT_IDS` | `GLOBAL_EXCLUDE_CHAT_IDS` |
| `PRIVATE_INCLUDE_CHAT_IDS`, `PRIVATE_EXCLUDE_CHAT_IDS` | the same names |
| `GROUPS_INCLUDE_CHAT_IDS`, `GROUPS_EXCLUDE_CHAT_IDS` | the same names |
| `CHANNELS_INCLUDE_CHAT_IDS`, `CHANNELS_EXCLUDE_CHAT_IDS` | the same names |
| `PRIORITY_CHAT_IDS` | `PRIORITY_CHAT_IDS` |
| `SKIP_MEDIA_CHAT_IDS` | `SKIP_MEDIA_CHAT_IDS` |
| `INCLUDE_FOLDER_IDS` | `GLOBAL_INCLUDE_FOLDER_IDS` |
| `PRIVATE_INCLUDE_FOLDER_IDS`, `GROUPS_INCLUDE_FOLDER_IDS`, `CHANNELS_INCLUDE_FOLDER_IDS` | the same names |

How a value resolves for one account:

- A value in the indexed variable wins for that account.
- An empty or unset indexed variable inherits the global value.
- The literal `none`, in any case, means an explicitly empty list.
- An override for an account number that is not declared stops startup.
- A non-integer entry in an indexed id list stops startup with a generic Python error that does not name the variable.

This example backs up everything for account 1 and only channels for account 2:

```dotenv
CHAT_TYPES=private,groups,channels
TG_ACCOUNT_2_CHAT_TYPES=channels
```

A global include list turns off `CHAT_TYPES`. If you set `GLOBAL_INCLUDE_CHAT_IDS`, also set `TG_ACCOUNT_2_INCLUDE_CHAT_IDS=none` so account 2 does not inherit it. How the filters combine is explained in [Choosing chats](choosing-chats.md).

!!! warning "Folder ids are per account"
    Telegram numbers folders separately in each account: folder 3 of one account has nothing to do with folder 3 of another. This applies to `GLOBAL_INCLUDE_FOLDER_IDS` and to the `PRIVATE_`, `GROUPS_` and `CHANNELS_` folder lists. With more than one account, startup stops if two or more accounts would inherit one of these without a prefix. Scope it per account, or give the other accounts `none`:

    ```dotenv
    GLOBAL_INCLUDE_FOLDER_IDS=3
    TG_ACCOUNT_2_INCLUDE_FOLDER_IDS=none
    ```

## Settings that stay global

Everything that is not a chat filter applies to all accounts. That includes `DOWNLOAD_MEDIA`, `MAX_MEDIA_SIZE_MB`, the listener toggles, `SKIP_TOPIC_IDS` and the transcription settings.

## How runs work

- The scheduler opens one shared Telegram connection per account.
- Accounts are backed up one after another, in configuration order. A run takes about as long as all the accounts' runs added together.
- If one account fails, the error is logged with its index and the other accounts continue.
- After login, the archive matches each account to its data row by its Telegram user id. Changing the order of the `TG_ACCOUNT_<N>` numbers does not move data between accounts.
- All accounts share one owner id, one last backup time and one "backup running" flag. Each value shows whichever account updated it last.
- With `ENABLE_LISTENER=true`, each account gets its own listener. See [Real-time listener](listener.md).

## In the viewer

A small label with the account name, called an account chip, appears on chat rows, in the chat header and in the info panel. Chips only appear when the logged-in user can see more than one account. On a phone the header hides its chips; the chat list and the info panel keep them.

![Chat list with Personal and Work account chips](../images/screenshots/chat-list-desktop.png)

- A group or channel that several archived accounts belong to is listed once. The viewer shows the copy from the lowest account id the user may see.
- Private chats are never merged.
- A message sent by any archived account shows as your own message, with that account's chip.
- Viewer accounts can be limited to some accounts. See [Logins, viewer accounts and share links](../viewer/access.md).

## Caveats

- Imports from Telegram Desktop always land in account 1.
- `backfill-topics` sets `CHAT_IDS` to the chat you ask for. If an account sets `TG_ACCOUNT_<N>_CHAT_IDS`, the command uses that list for the account and ignores the chat you asked for.
- `scripts/auth_noninteractive.py` only reads `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` and `TELEGRAM_PHONE`. It cannot log in indexed accounts.
- There is no supported way to merge two existing archives on a current release.

See [Import and maintenance tasks](../operations/maintenance.md) for imports and `backfill-topics`, and [Upgrading](../operations/upgrading.md) for moving from 7.x.
