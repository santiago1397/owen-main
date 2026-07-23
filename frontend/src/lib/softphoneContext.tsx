// App-wide single softphone instance (Ticket 18).
//
// Before this, each <InCallBar> called useSoftphone() independently, so the availability
// toggle and any incoming call only existed on whichever page was mounted. For the incoming-
// call popup to work from ANY page (Quo-style), the softphone must be ONE instance lifted to
// the app shell. This provider owns that instance; InCallBar and the global IncomingCallModal
// both read it via useSoftphoneContext().
import { createContext, useContext } from "react";
import { useSoftphone, type SoftphoneApi } from "./softphone";

const SoftphoneContext = createContext<SoftphoneApi | null>(null);

export function SoftphoneProvider({ children }: { children: any }) {
  const softphone = useSoftphone();
  return <SoftphoneContext.Provider value={softphone}>{children}</SoftphoneContext.Provider>;
}

export function useSoftphoneContext(): SoftphoneApi {
  const ctx = useContext(SoftphoneContext);
  if (!ctx) throw new Error("useSoftphoneContext must be used within a SoftphoneProvider");
  return ctx;
}
