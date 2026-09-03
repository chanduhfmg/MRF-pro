import { AnimatePresence } from "framer-motion";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import AuroraBackground from "./components/AuroraBackground";
import Sidebar from "./components/Sidebar";
import { useAsync } from "./hooks/useAsync";
import { api } from "./lib/api";
import UploadPage from "./pages/UploadPage";
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
          <AnimatePresence mode="wait">
            <Routes location={location} key={location.pathname}>
              <Route path="/" element={<Navigate to="/dashboard" replace />} />
              <Route path="/upload" element={<UploadPage meta={meta.data} />} />
              <Route path="/jobs" element={<JobsPage />} />
              <Route path="/jobs/:id" element={<JobDetailPage />} />
              <Route path="/dashboard" element={<DashboardPage />} />
              <Route path="*" element={<Navigate to="/dashboard" replace />} />
            </Routes>
          </AnimatePresence>
        </div>
      </main>
    </div>
  );
}
