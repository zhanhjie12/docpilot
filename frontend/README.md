# DocPilot frontend

This is the React + TypeScript + Vite workbench for DocPilot's document workflows.

## Run

```powershell
npm install
npm run dev
```

如果 Windows PowerShell 因执行策略阻止 `npm.ps1`，使用对应的命令行入口：

```powershell
npm.cmd install
npm.cmd run dev
npm.cmd run build -- --configLoader native
```

The API client defaults to `http://localhost:8000/api/v1`. Set
`VITE_API_BASE` when the backend is hosted elsewhere, for example:

```powershell
$env:VITE_API_BASE = "http://localhost:9000/api/v1"
npm run dev
```

The page supports typed document upload, task polling, rule based contract
review, cited document questions, effective policy questions, and tender/bid
evidence alignment. It restores the active review after a page refresh by
rechecking opaque IDs with the backend. Model output remains a draft until
reviewed against source evidence.
