export async function scanProject(folderPath: string) {
  const response = await fetch('/api/scan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder_path: folderPath })
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to scan project');
  }
  return response.json();
}

export async function updatePackage(folderPath: string, packageInfo: object) {
  const response = await fetch('/api/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder_path: folderPath, package_info: packageInfo })
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to update package');
  }
  return response.json();
}

export async function rollbackPackage(checkpointId: string, folderPath: string) {
  const response = await fetch('/api/rollback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ checkpoint_id: checkpointId, folder_path: folderPath })
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to rollback package');
  }
  return response.json();
}
