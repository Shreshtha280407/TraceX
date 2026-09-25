import { create } from 'zustand'

interface WorkspaceSelectionState {
  /** Synced between Investigation Workspace and Timeline + Map (Section 8:
   * "synced to the same case/entity selection state as the Workspace").
   * Deliberately not persisted -- unlike `activeCaseId` (real ABAC scoping),
   * this is ephemeral UI convenience that should reset on a fresh session. */
  selectedEntityId: string | null
  setSelectedEntityId: (entityId: string | null) => void
}

export const useWorkspaceSelectionStore = create<WorkspaceSelectionState>()((set) => ({
  selectedEntityId: null,
  setSelectedEntityId: (entityId) => set({ selectedEntityId: entityId }),
}))
