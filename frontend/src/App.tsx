import { AnimatePresence } from "framer-motion";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import AuroraBackground from "./components/AuroraBackground";
import Sidebar from "./components/Sidebar";
import { useAsync } from "./hooks/useAsync";
import { api } from "./lib/api";
import SearchPage from "./pages/SearchPage";
import ProviderLookupPage from "./pages/ProviderLookupPage";
import UploadPage from "./pages/UploadPage";
import ColumnsPage from "./pages/ColumnsPage";
import ConvertPage from "./pages/ConvertPage";
import JobsPage from "./pages/JobsPage";
import JobDetailPage from "./pages/JobDetailPage";
import DashboardPage from "./pages/DashboardPage";

export default function App() {
  const location = useLocation();
  const meta = useAsync(() => api.meta(), []);

  return (
    <div className="relative flex min-h-screen">
      <AuroraBackground />
      <Sidebar extractionMode={meta.data?.extraction_mode} />

      <main className="min-w-0 flex-1">
        <div className="mx-auto max-w-[1180px] px-6 py-8 lg:px-10 lg:py-10">
          <AnimatePresence mode="sync">
            <Routes location={location} key={location.pathname}>
              {/* Search is the front door: the usual path is find-then-extract,
                  with manual upload as the fallback for a file you already have. */}
              <Route path="/" element={<Navigate to="/search" replace />} />
              <Route path="/search" element={<SearchPage meta={meta.data} />} />
              <Route path="/lookup" element={<ProviderLookupPage meta={meta.data} />} />
              <Route path="/upload" element={<UploadPage meta={meta.data} />} />
              <Route path="/columns" element={<ColumnsPage meta={meta.data} />} />
              <Route path="/convert" element={<ConvertPage />} />
              <Route path="/jobs" element={<JobsPage />} />
              <Route path="/jobs/:id" element={<JobDetailPage />} />
              <Route path="/dashboard" element={<DashboardPage />} />
              <Route path="*" element={<Navigate to="/search" replace />} />
            </Routes>
          </AnimatePresence>
        </div>
      </main>
    </div>
  );
}
