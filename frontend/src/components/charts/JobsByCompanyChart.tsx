import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { CompanyCount } from "../../lib/types";
import { AXIS, ChartTooltip } from "./ChartFrame";
import { formatCompact } from "../../lib/format";

const BARS = ["#1a5c38", "#2f9e5f", "#3ddc84", "#7ff0b3", "#0f5f5c", "#15908a"];

export default function JobsByCompanyChart({ data }: { data: CompanyCount[] }) {
  const rows = data.map((d) => ({ name: d.company_label.replace(/\s*\(.*\)/, ""), jobs: d.jobs, records: d.records }));

  if (!rows.length) {
    return <div className="grid h-full place-items-center text-xs text-faint">No jobs yet</div>;
  }

  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={rows} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid vertical={false} stroke="rgba(255,255,255,0.06)" />
        <XAxis dataKey="name" {...AXIS} interval={0} />
        <YAxis {...AXIS} allowDecimals={false} width={32} tickFormatter={(v) => formatCompact(v)} />
        <Tooltip
          cursor={{ fill: "rgba(255,255,255,0.04)" }}
          content={<ChartTooltip />}
        />
        <Bar dataKey="jobs" radius={[6, 6, 0, 0]} maxBarSize={64}>
          {rows.map((_, i) => (
            <Cell key={i} fill={BARS[i % BARS.length]} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}
