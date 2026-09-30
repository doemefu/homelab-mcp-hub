# GitHub Project — Progress Tracking

All work is tracked in the homelab GitHub Project #5:
https://github.com/users/doemefu/projects/5

**Project ID:** `PVT_kwHOBwXMZc4BStGq`

## Status Field

| Status | Meaning | Required at | Option ID |
|--------|---------|-------------|-----------|
| Backlog | Raw idea, not yet scoped | — | `f75ad846` |
| Ready | Scoped and described, plan not started | — | `61e4505c` |
| Planned | Plan written + reviewed, ready to implement | End of Phase 3 | `913a2fb0` |
| In implementation | Actively being built | Start of Phase 4 | `47fc9ee4` |
| In review | Implementation done, under review | Start of Phase 5 | `df73e18b` |
| Done | Shipped | End of Phase 6 | `98236657` |

> To get current option IDs (including Planned):
> ```bash
> gh project field-list 5 --owner doemefu --format json | \
>   python3 -c "import json,sys; [print(o['name'], o['id']) for f in json.load(sys.stdin)['fields'] if f['name']=='Status' for o in f.get('options',[])]"
> ```

## How to Find a Project Item ID

```bash
gh project item-list 5 --owner doemefu --format json | \
  python3 -c "import json,sys; [print(i['id'], i['title'][:60]) for i in json.load(sys.stdin)['items'] if 'KEYWORD' in i['title']]"
```

## How to Update Status

```bash
gh project item-edit \
  --id <ITEM_ID> \
  --project-id PVT_kwHOBwXMZc4BStGq \
  --field-id PVTSSF_lAHOBwXMZc4BStGqzhAKTIc \
  --single-select-option-id <OPTION_ID>
```

## Required Phase Transitions

| Phase completed | GitHub status |
|-----------------|---------------|
| Phase 3 — plan reviewed | → **Planned** |
| Phase 4 — implementation starts | → **In implementation** |
| Phase 5 — review starts | → **In review** |
| Phase 6 — shipped | → **Done** |

Always update the corresponding issue's status **before moving to the next phase**.
