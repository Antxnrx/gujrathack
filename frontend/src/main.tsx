import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { ToastHost } from "./components/Toasts";
import { loadToken } from "./lib/api";
import "./styles.css";

loadToken();

/**
 * `refetchOnWindowFocus` is off deliberately.
 *
 * The default is right for a tab someone leaves for an hour. It is wrong for a control
 * room: the console shares a screen with a radio log and a phone, so it loses and regains
 * focus dozens of times an hour, and each regain would refire every mounted query — the
 * timeline, the summary, the alert queue, the camera estate. The live bus already pushes
 * what changed, so focus carries no information here.
 */
const qc = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      staleTime: 15_000,
      gcTime: 10 * 60_000,
      retry: (count, err) => {
        const s = (err as { status?: number })?.status ?? 0;
        // A 4xx is an answer, not a failure to reach. Retrying it wastes a round trip
        // and delays showing the operator the 422 that names the field they got wrong.
        if (s >= 400 && s < 500) return false;
        return count < 2;
      },
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={qc}>
      <BrowserRouter>
        <ToastHost>
          <App />
        </ToastHost>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
