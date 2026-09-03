import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { VolumePoint } from "../../lib/types";
import { AXIS, ChartTooltip } from "./ChartFrame";
import { formatCompact } from "../../lib/format";

type Metric = "records" | "jobs";

export default function VolumeChart({
  data,
  metric = "records",
}: {
  data: VolumePoint[];
  metric?: Metric;
}) {
  const rows = data.map((d) => ({
    date: new Date(d.date + "T00:00:00Z").toLocaleDateString("en-US", { month: "short", day: "numeric" }),
    records: d.records,
    jobs: d.jobs,
  }));

  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={rows} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id="volFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#3ddc84" stopOpacity={0.35} />
            <stop offset="100%" stopColor="#1a5c38" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid vertical={false} stroke="rgba(255,255,255,0.06)" />
        <XAxis
          dataKey="date"
          {...AXIS}
          minTickGap={28}
        />
        <YAxis {...AXIS} width={52} allowDecimals={false} tickFormatter={(v) => formatCompact(v)} />
        <Tooltip content={<ChartTooltip formatter={(v) => v.toLocaleString("en-US")} />} />
        <Area
          type="monotone"
          dataKey={metric}
          stroke="#3ddc84"
          strokeWidth={2}
          fill="url(#volFill)"
          activeDot={{ r: 3.5, strokeWidth: 0 }}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
