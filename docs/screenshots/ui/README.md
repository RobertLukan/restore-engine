# UI screenshots

Captured from the lab UI with **private fields blurred** (names, IDs, meta rows, form inputs) via `scripts/capture-ui-shots.sh`.

| File | View |
|------|------|
| `01-restores.png` | Restores queue + jobs |
| `02-groups.png` | Groups |
| `03-locations.png` | Locations |
| `04-plans.png` | Plans |
| `05-assurance.png` | Assurance |
| `06-help.png` | Help |
| `07-settings.png` | Settings |
| `08-help-sidebar-collapsed.png` | Help with collapsed sidebar |
| `09-cleanup.png` | Cleanup (batch destroy) |
| `10-compliance.png` | Compliance |
| `11-backups.png` | Backups inventory |

Re-capture (Mac lab / host-network UI on `:8000`):

```bash
cd /path/to/restore-engine
RE_UI_URL=http://127.0.0.1:8000 ./scripts/capture-ui-shots.sh
```
