# Optional Google Drive upload

`publish_drive.py` uploads a normalized JSON export and updates the same Drive file
on later runs. Google Drive is optional; browser capture works without it.

The publisher accepts `study_sync.py export` output, not browser captures. It does
not update a ChatGPT project or configure a study schedule. Live Google sign-in and
upload have not been verified.

## Setup

1. In Google Cloud, enable the Drive API and create a Desktop app OAuth client.
   Configure consent and test-user access for the Google account you will use.
2. Save the downloaded client configuration as `private/google_client.json`.
3. From the project directory, install the optional dependencies into the virtual
   environment created in the [setup guide](../README.md):

```text
.venv\Scripts\python.exe -m pip install -r requirements-drive.txt
```

Credentials are stored in the operating system's keyring. A working secure keyring
is required. Keep client configuration, tokens, and exports private.

## Upload an export

After creating the [synthetic demo export](../FORMAT.md#save-a-synthetic-export),
run this command to sign in and upload it to your Google Drive:

```text
.venv\Scripts\python.exe publish_drive.py private/demo_state.json --client private/google_client.json --authorize --mapping private/demo_drive_mapping.json --profile demo
```

This command transmits the demo file. The script requests the `drive.file` scope
and does not add sharing permissions. It prints the resulting file ID and, when
available, its link. The local mapping file preserves the ID for later updates.

For subsequent uploads, use the same command without `--authorize`. If Google
authorization expires or is revoked, rerun with `--authorize` to sign in again.
Keep the mapping file so later uploads can identify the same destination.

Use separate databases, exports, mappings, and credential profiles for demo data
and each real learner. Run one upload at a time for each mapping.
