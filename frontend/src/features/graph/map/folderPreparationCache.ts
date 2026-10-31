import type { WorkspaceGraphScene } from "../../../shared/api/workspaceGraph";
import {
  prepareWorkspaceMapFolderScene,
  type WorkspaceMapFolderSceneOptions,
  type WorkspaceMapFolderSceneResult,
} from "./folderScene";

// Preparing sorts and hashes every folder relation (about a second at 10K), so
// a scene pays it once, on the first enable. The key is the scene object: each
// new viewer, workspace, request, scale or expansion response is a new object,
// and the entry goes with its scene, so nothing is shared across requests. The
// one caller always passes the same budgets, so they are not part of the key.
const preparations = new WeakMap<WorkspaceGraphScene, WorkspaceMapFolderSceneResult>();

export function prepareWorkspaceMapFolderSceneOnce(
  scene: WorkspaceGraphScene,
  options: WorkspaceMapFolderSceneOptions = {},
): WorkspaceMapFolderSceneResult {
  const cached = preparations.get(scene);
  if (cached) return cached;
  const result = prepareWorkspaceMapFolderScene(scene, options);
  preparations.set(scene, result);
  return result;
}
