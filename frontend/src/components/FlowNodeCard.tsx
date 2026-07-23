import { Handle, Position, type NodeProps } from "@xyflow/react";
import { NODE_TITLES, type CanvasNode, nodePorts, nodeSummary } from "../lib/canvasGraph";

// The on-canvas card for every flow node type (Ticket 16). Renders the node's type, its
// operator-facing label, a one-line config summary, and one SOURCE handle per validator
// port (right edge); every non-entry node has a single TARGET handle (left edge).
export default function FlowNodeCard({ data, selected }: NodeProps<CanvasNode>) {
  const ports = nodePorts(data.ntype, data.config);
  return (
    <div className={"flownode" + (selected ? " sel" : "") + ` fn-${data.ntype}`}>
      {data.ntype !== "entry" && (
        <Handle type="target" position={Position.Left} className="fn-handle" />
      )}
      <div className="fn-head">
        <span className={`fn-type fn-type-${data.ntype}`}>{NODE_TITLES[data.ntype]}</span>
        {data.config.label ? <span className="fn-label">{data.config.label}</span> : null}
      </div>
      <div className="fn-summary">{nodeSummary(data.ntype, data.config)}</div>
      {ports.length > 0 && (
        <div className="fn-ports">
          {ports.map((p) => (
            <div key={p} className="fn-port">
              <span className="fn-portname">{p}</span>
              <Handle
                id={p}
                type="source"
                position={Position.Right}
                className="fn-handle fn-out"
              />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
