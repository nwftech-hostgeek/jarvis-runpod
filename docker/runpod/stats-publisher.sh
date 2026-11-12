#!/bin/sh
# Writes a sanitized public stats.json for the Jarvis orb HUD.
# No secrets, keys, tokens, hostnames with credentials, or model paths.
set -eu

data_root=${RUNPOD_DATA_ROOT:-/workspace/jarvis-data}
out_dirs="${data_root}/jarvis-orb"
interval=${JARVIS_STATS_INTERVAL:-2}

probe() {
    url=$1
    if curl --fail --silent --show-error --max-time 1 "${url}" >/dev/null 2>&1; then
        printf 'ok'
    else
        printf 'down'
    fi
}

read_mem() {
    # MemAvailable / MemTotal from /proc/meminfo (kB)
    awk '
      /^MemTotal:/ { t=$2 }
      /^MemAvailable:/ { a=$2 }
      END {
        if (t > 0) {
          used = t - a
          printf "{\"total_mb\":%d,\"used_mb\":%d,\"used_pct\":%.1f}", t/1024, used/1024, (used*100)/t
        } else {
          printf "{\"total_mb\":0,\"used_mb\":0,\"used_pct\":0}"
        }
      }' /proc/meminfo 2>/dev/null || printf '{"total_mb":0,"used_mb":0,"used_pct":0}'
}

read_cpu() {
    # Rough 1-sample load from /proc/stat + loadavg
    load=$(awk '{printf "%s", $1}' /proc/loadavg 2>/dev/null || printf '0')
    nproc=$(nproc 2>/dev/null || printf '1')
    awk -v load="${load}" -v n="${nproc}" 'BEGIN {
      pct = 0
      if (n > 0) pct = (load / n) * 100
      if (pct > 100) pct = 100
      printf "{\"load1\":%s,\"cpus\":%s,\"approx_pct\":%.1f}", load, n, pct
    }'
}

read_disk() {
    df -P "${data_root}" 2>/dev/null | awk 'NR==2 {
      total=$2; used=$3; avail=$4; pct=$5
      gsub(/%/,"",pct)
      printf "{\"total_gb\":%.1f,\"used_gb\":%.1f,\"free_gb\":%.1f,\"used_pct\":%s}", total/1048576, used/1048576, avail/1048576, pct
      found=1
    } END { if (!found) printf "{\"total_gb\":0,\"used_gb\":0,\"free_gb\":0,\"used_pct\":0}" }'
}

read_gpu() {
    if ! command -v nvidia-smi >/dev/null 2>&1; then
        printf '{"available":false}'
        return
    fi
    # Query only utilization / memory — never UUIDs or serials in public JSON.
    line=$(nvidia-smi --query-gpu=utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu --format=csv,noheader,nounits 2>/dev/null | head -n1 || true)
    if [ -z "${line}" ]; then
        printf '{"available":false}'
        return
    fi
    printf '%s' "${line}" | awk -F',' '{
      gsub(/^ +| +$/,"",$1); gsub(/^ +| +$/,"",$2);
      gsub(/^ +| +$/,"",$3); gsub(/^ +| +$/,"",$4); gsub(/^ +| +$/,"",$5);
      printf "{\"available\":true,\"util_pct\":%s,\"mem_util_pct\":%s,\"vram_used_mb\":%s,\"vram_total_mb\":%s,\"temp_c\":%s}", $1+0, $2+0, $3+0, $4+0, $5+0
    }'
}

read_uptime() {
    awk '{printf "%d", $1}' /proc/uptime 2>/dev/null || printf '0'
}

read_boot_utc() {
    boot_file="${data_root}/jarvis-orb/boot-datetime.json"
    if [ -f "${boot_file}" ]; then
        # Extract ISO stamp without jq; file is ours and small.
        awk -F'"' '/process_started_at_utc/ {print $4; exit}' "${boot_file}" 2>/dev/null || true
    fi
}

publish_once() {
    ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    weekday_utc=$(date -u +"%A")
    gpu=$(read_gpu)
    mem=$(read_mem)
    cpu=$(read_cpu)
    disk=$(read_disk)
    up=$(read_uptime)
    ui=$(probe "http://127.0.0.1:${RUNPOD_UI_PORT:-3000}/health")
    llama=$(probe "http://127.0.0.1:8080/health")
    voice=$(probe "http://127.0.0.1:8000/health")
    tools=$(probe "http://127.0.0.1:8090/health")
    boot_utc=$(read_boot_utc)
    if [ -z "${boot_utc}" ]; then
        boot_utc="${ts}"
    fi

    payload=$(printf '%s' "{
  \"ts\": \"${ts}\",
  \"weekday_utc\": \"${weekday_utc}\",
  \"process_started_at_utc\": \"${boot_utc}\",
  \"preferred_local_timezone\": \"America/New_York\",
  \"uptime_sec\": ${up},
  \"services\": {
    \"open_webui\": \"${ui}\",
    \"llama\": \"${llama}\",
    \"voice\": \"${voice}\",
    \"tools\": \"${tools}\"
  },
  \"cpu\": ${cpu},
  \"memory\": ${mem},
  \"disk\": ${disk},
  \"gpu\": ${gpu}
}")

    mkdir -p "${out_dirs}/assets"
    printf '%s\n' "${payload}" > "${out_dirs}/stats.json.tmp"
    mv -f "${out_dirs}/stats.json.tmp" "${out_dirs}/stats.json"

    # Mirror into live Open WebUI static trees when present.
    ow_root=""
    if [ -x /opt/open-webui/bin/python ]; then
        ow_root="$(/opt/open-webui/bin/python -c 'import open_webui, os; print(os.path.dirname(open_webui.__file__))' 2>/dev/null || true)"
    fi
    if [ -n "${ow_root}" ]; then
        for dest in \
            "${ow_root}/static/jarvis-orb" \
            "${ow_root}/frontend/jarvis-orb"
        do
            if [ -d "${dest}" ]; then
                cp -f "${out_dirs}/stats.json" "${dest}/stats.json" 2>/dev/null || true
                if [ -f "${out_dirs}/boot-datetime.json" ]; then
                    cp -f "${out_dirs}/boot-datetime.json" "${dest}/boot-datetime.json" 2>/dev/null || true
                fi
            fi
        done
    fi
}

while true; do
    publish_once || true
    sleep "${interval}"
done
