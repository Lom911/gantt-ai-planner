import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";

export const CONFIRMATION_KEY = ["plan-confirmation"];

// The mass deletion waiting for approval, if any (refreshed by useSessionEvents on the
// confirmation_* events). A backend without the endpoint answers 404 → no banner.
export function useConfirmation() {
  return useQuery({ queryKey: CONFIRMATION_KEY, queryFn: api.pendingConfirmation, retry: false });
}
