#!/usr/bin/env python3
"""
Monitor Status Interface
Displays health status of local and remote LLM nodes.
"""

import json
import re
from datetime import datetime
from typing import List, Tuple

class NodeStatus:
    def __init__(self, name: str, ip: str, port: int, latency: float, healthy: bool):
        self.name = name
        self.ip = ip
        self.port = port
        self.latency = latency
        self.healthy = healthy
    
    def __str__(self):
        return f"{self.name} {self.ip}:{self.port} {self.latency:.3f} ms {'✓' if self.healthy else '✗'}"

class StatusDisplay:
    def __init__(self, data_file: str = "node_status.json"):
        self.data_file = data_file
        self.nodes: List[NodeStatus] = []
        
    def parse_status_line(self, line: str) -> Tuple[List[NodeStatus], bool]:
        """Parse status line into node statuses and recovery flag."""
        prefix = "All targets healthy:"
        line = line.strip()
        if not line.startswith(prefix):
            raise ValueError("Unsupported status summary; expected All targets healthy:")

        body = line[len(prefix):].strip()
        # Remove the report trailer before splitting nodes: it may follow ms.
        trailer = re.search(r"(?:[.;]\s*|\s+)No recovery action needed\.?$", body)
        if trailer is None:
            raise ValueError("Healthy summary requires No recovery action needed trailer")
        body = body[:trailer.start()].rstrip(" ;.")
        nodes = []
        pattern = re.compile(
            r"(?P<name>\S+)\s+(?P<host>\[[^\]]+\]|[^\s:]+):"
            r"(?P<port>[0-9]+)\s+(?P<latency>[0-9]+(?:\.[0-9]+)?)\s*ms"
        )
        for part in body.split(";"):
            match = pattern.fullmatch(part.strip())
            if match is None:
                raise ValueError("Malformed node entry in status summary")
            port = int(match["port"])
            if not 1 <= port <= 65535:
                raise ValueError("Node port must be between 1 and 65535")
            nodes.append(NodeStatus(
                match["name"], match["host"].strip("[]"), port,
                float(match["latency"]), True,
            ))
        return nodes, False

    def update_status(self, line: str):
        """Replace status only after the entire summary validates.

        Unsupported/unhealthy summaries raise rather than presenting old data
        as a newly successful check. Callers must surface this error.
        """
        nodes, _ = self.parse_status_line(line)
        self.nodes = nodes
        print("Status updated")

    def display(self):
        """Display current status in a formatted table."""
        print("=" * 60)
        print("LLM NODE STATUS MONITOR")
        print("=" * 60)
        print(f"Last Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("-" * 60)
        
        for node in self.nodes:
            print(f"{node.name:<12} {node.ip:<15} {node.port:>5} {node.latency:>8.3f} ms {'✓' if node.healthy else '✗'}")
        
        print("-" * 60)
        if not self.nodes:
            print("No status data available.")
    
    def save_to_file(self):
        """Save current status to JSON file."""
        data = {
            "timestamp": datetime.now().isoformat(),
            "nodes": [
                {
                    "name": node.name,
                    "ip": node.ip,
                    "port": node.port,
                    "latency": node.latency,
                    "healthy": node.healthy
                }
                for node in self.nodes
            ]
        }
        
        with open(self.data_file, "w") as f:
            json.dump(data, f, indent=2)
    
    def load_from_file(self):
        """Load status from JSON file."""
        try:
            with open(self.data_file, "r") as f:
                data = json.load(f)
                
            self.nodes = [
                NodeStatus(
                    node["name"],
                    node["ip"],
                    node["port"],
                    node["latency"],
                    node["healthy"]
                )
                for node in data.get("nodes", [])
            ]
        except FileNotFoundError:
            pass

def main():
    # Example input line (you'd typically read this from a stream or file)
    status_line = "All targets healthy: local 127.0.0.1:8000 10.5 ms; sample 192.0.2.10:11434 20.0 ms. No recovery action needed."
    
    # Create display instance
    display = StatusDisplay()
    
    # Update and display status
    display.update_status(status_line)
    display.display()
    
    # Save to file for backend processing
    display.save_to_file()

if __name__ == "__main__":
    main()