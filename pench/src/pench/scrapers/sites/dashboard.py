import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import os
import re
import sys
import subprocess
from datetime import datetime, timezone, timedelta
import requests
import time
from dotenv import load_dotenv
from pathlib import Path

# Page config
st.set_page_config(
    page_title="Pench Alerts Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS
st.markdown("""
<style>
    .main-header {
        font-size: 2.5rem;
        font-weight: bold;
        text-align: center;
        background: linear-gradient(90deg, #667eea 0%, #764ba2 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        padding: 0.5rem;
    }
    .section-header {
        font-size: 1.5rem;
        font-weight: bold;
        color: #667eea;
        margin-top: 1.5rem;
        margin-bottom: 1rem;
        border-bottom: 3px solid #667eea;
        padding-bottom: 0.5rem;
    }
    .metric-highlight {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        padding: 1rem;
        border-radius: 8px;
        color: white;
    }
    .insight-card {
        background: #f8f9fa;
        border-left: 4px solid #667eea;
        padding: 1rem;
        margin: 0.5rem 0;
        border-radius: 4px;
    }
    .success-highlight { border-left-color: #28a745; }
    .warning-highlight { border-left-color: #ffc107; }
    .danger-highlight { border-left-color: #dc3545; }
</style>
""", unsafe_allow_html=True)

# Helper function for Slack API calls (used multiple times)
def slack_get(api_url, headers, method, params):
    url = f"{api_url}/{method}"
    r = requests.get(url, headers=headers, params=params, timeout=60)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method} failed: {data.get('error')}")
    return data

# Extract text from Slack message (used multiple times in loop)
def get_message_text(m):
    parts = []
    if m.get("text"):
        parts.append(m["text"])
    
    for b in m.get("blocks", []) or []:
        bt = b.get("type")
        if bt == "section":
            t = (b.get("text") or {}).get("text")
            if t:
                parts.append(t)
        elif bt == "header":
            t = (b.get("text") or {}).get("text")
            if t:
                parts.append(t)
        elif bt == "context":
            for el in b.get("elements", []) or []:
                t = el.get("text")
                if t:
                    parts.append(t)
    
    for a in m.get("attachments", []) or []:
        for k in ("pretext", "title", "text", "footer"):
            v = a.get(k)
            if v:
                parts.append(v)
    
    seen = set()
    out = []
    for p in parts:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return "\n".join(out)

@st.cache_data(ttl=300)
def export_slack_data(channel_name, start_date, start_hour, end_date, end_hour, tz_offset="+05:30"):
    load_dotenv()
    SLACK_TOKEN = os.getenv("SLACK_BOT_TOKEN")
    
    if not SLACK_TOKEN:
        st.error("Missing SLACK_BOT_TOKEN in .env file")
        return None
    
    API = "https://slack.com/api"
    HEADERS = {"Authorization": f"Bearer {SLACK_TOKEN}"}
    
    # Parse timezone offset
    sign = 1 if tz_offset.startswith("+") else -1
    hh, mm = tz_offset[1:].split(":")
    TZ = timezone(timedelta(hours=sign * int(hh), minutes=sign * int(mm)))
    
    # Convert dates to unix timestamps
    dt_start = datetime.strptime(start_date, "%Y-%m-%d").replace(
        hour=start_hour, minute=0, second=0, microsecond=0, tzinfo=TZ
    )
    oldest = dt_start.timestamp()
    
    dt_end = datetime.strptime(end_date, "%Y-%m-%d").replace(
        hour=end_hour, minute=59, second=59, microsecond=999999, tzinfo=TZ
    )
    latest = dt_end.timestamp()
    
    # Get channel ID
    data = slack_get(API, HEADERS, "conversations.list", {
        "limit": 100,
        "exclude_archived": True,
        "types": "public_channel,private_channel",
    })

    channel_id = None
    for ch in data.get("channels", []):
        if ch.get("name") == channel_name.lstrip("#"):
            channel_id = ch["id"]
            break

    if not channel_id:
        st.error(f"Channel not found: {channel_name}")
        return None
    
    # Fetch messages
    messages = []
    cursor = None
    while True:
        data = slack_get(API, HEADERS, "conversations.history", {
            "channel": channel_id,
            "limit": 1000,
            "cursor": cursor or "",
            "oldest": oldest,
            "latest": latest,
            "inclusive": True,
        })
        messages.extend(data.get("messages", []))
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.6)
    
    # Sort messages
    msgs_sorted = sorted(messages, key=lambda m: float(m.get("ts", "0")))
    
    # Pre-compile regex patterns for better performance
    scraper_pattern = re.compile(r'Scraper `([^`]+)` failed')
    proxy_pattern = re.compile(r':lock: Proxy: ([^\n]+)')
    upi_pattern = re.compile(r'✅ SUCCESS - UPI extracted: ([^\n`]+)')
    
    # Extract required fields from messages
    data = []
    for m in msgs_sorted:
        ts = float(m.get("ts", "0"))
        dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
        dt_ist = dt_utc.astimezone(TZ).replace(tzinfo=None)
        
        text = get_message_text(m)
        text_lower = text.lower()  # Cache lowercased version
        
        # Extract scraper name
        scraper_match = scraper_pattern.search(text)
        scraper = scraper_match.group(1) if scraper_match else None
        
        # Extract proxy
        proxy_match = proxy_pattern.search(text)
        proxy = proxy_match.group(1).strip() if proxy_match else None
        
        # Check if success
        is_success = '✅ SUCCESS - UPI extracted:' in text
        
        # Extract UPI if success
        upi_extracted = None
        upi_provider = 'unknown'
        if is_success:
            upi_match = upi_pattern.search(text)
            if upi_match:
                upi_extracted = upi_match.group(1).split("\\n:")[0].strip()
                if upi_extracted and '@' in upi_extracted:
                    upi_provider = upi_extracted.split('@')[-1]
        
        # Determine error type - optimized with cached lowercase and early checks
        if is_success:
            error_type = 'Success'
        elif 'no upi extracted' in text_lower:
            error_type = 'No UPI Extracted'
        elif 'timeout' in text_lower:
            if 'locator' in text_lower:
                error_type = 'Element Timeout'
            elif 'scrape exceeded' in text_lower:
                error_type = 'Scrape Timeout'
            else:
                error_type = 'Page Timeout'
        elif 'err_' in text_lower:  # Group common ERR_ patterns
            if 'err_empty_response' in text_lower:
                error_type = 'Empty Response'
            elif 'err_tunnel_connection_failed' in text_lower:
                error_type = 'Tunnel Failed'
            elif 'err_connection_closed' in text_lower:
                error_type = 'Connection Closed'
            elif 'err_connection_refused' in text_lower:
                error_type = 'Connection Refused'
            elif 'err_proxy_connection_failed' in text_lower:
                error_type = 'Proxy Connection Failed'
            elif 'err_timed_out' in text_lower:
                error_type = 'Timed Out'
            else:
                error_type = 'Other'
        elif 'net::' in text:
            error_type = 'Network Error'
        else:
            error_type = 'Other'
        
        data.append({
            "datetime_ist": dt_ist,
            "scraper": scraper,
            "proxy": proxy,
            "is_success": is_success,
            "upi_extracted": upi_extracted,
            "upi_provider": upi_provider,
            "error_type": error_type
        })
    
    return pd.DataFrame(data)

# Main app
def run_app():
    st.markdown('<div class="main-header">Pench Alerts Dashboard</div>', unsafe_allow_html=True)
    
    # Sidebar
    with st.sidebar:
        st.header("Configuration")
        
        channel_name = st.text_input("Slack Channel", value="pench-alerts")
        
        st.subheader("Date & Time Range")
        today = datetime.now().date()
        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input("Start Date", value=today)
            start_hour = st.slider("Start Hour", 0, 23, 0)
        with col2:
            end_date = st.date_input("End Date", value=today)
            end_hour = st.slider("End Hour", 0, 23, 23)
        
        tz_offset = "+05:30"
        
        st.markdown("---")
        refresh_button = st.button("Fetch Data", type="primary", use_container_width=True)
        
    
    # Data loading
    df = None
    
    if refresh_button:
        export_slack_data.clear()
        if 'df' in st.session_state:
            del st.session_state['df']
        with st.spinner("Fetching data from Slack..."):
            df = export_slack_data(
                channel_name,
                start_date.strftime("%Y-%m-%d"),
                start_hour,
                end_date.strftime("%Y-%m-%d"),
                end_hour,
                tz_offset
            )
            if df is not None and not df.empty:
                st.session_state['df'] = df
    elif 'df' in st.session_state:
        df = st.session_state['df'].copy()
    
    if df is None or df.empty:
        st.info("Configure settings in sidebar and click **Fetch Data**")
        return
    
    df = df.copy()
    df['datetime_ist'] = pd.to_datetime(df['datetime_ist'])
    df['date'] = df['datetime_ist'].dt.date
    df['hour'] = df['datetime_ist'].dt.hour
    
    # Get list of site names from sites/working directory
    working_sites = set()
    sites_dir = Path(__file__).parent / "working"
    if sites_dir.exists():
        for file in sites_dir.glob("site_*.py"):
            content = file.read_text(encoding='utf-8')
            matches = re.findall(r'^\s+name\s*[:\s]*(?:str\s*)?=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
            if matches:
                working_sites.add(matches[0])
    
    sites_in_data = set(df['scraper'].dropna().unique())
    missing_sites = sites_in_data - working_sites
    
    if missing_sites:
        with st.expander(f"⚠️ Filtered {len(missing_sites)} sites missing from sites/working/", expanded=False):
            st.warning(f"{len(missing_sites)} sites removed (no scraper files in `sites/working/`):")
            missing_list = sorted(missing_sites)
            for i in range(0, len(missing_list), 3):
                cols = st.columns(3)
                for j in range(3):
                    if i + j < len(missing_list):
                        site = missing_list[i + j]
                        site_count = len(df[df['scraper'] == site])
                        cols[j].markdown(f"- `{site}` ({site_count})")
        
        df = df[~df['scraper'].isin(missing_sites)].copy()
    
    # Check if dataframe is empty after filtering
    if df.empty:
        st.warning("⚠️ No data available after filtering. All scrapers have been filtered out.")
        st.info("💡 Add scraper files to `sites/working/` directory to see data here.")
        return
    
    # Check if datetime column has valid values
    if df['datetime_ist'].isna().all():
        st.error("❌ No valid datetime data found. Cannot display dashboard.")
        return
    
    df_success = df[df['is_success']].copy()
    df_failures = df[~df['is_success']].copy()
    total_records = len(df)
    success_count = df['is_success'].sum()
    fail_count = total_records - success_count
    success_rate = (success_count / total_records * 100) if total_records > 0 else 0
    unique_sites = df['scraper'].nunique()
    unique_proxies = df['proxy'].nunique()
    unique_upis = df_success['upi_extracted'].nunique() if len(df_success) > 0 else 0
    total_upis = len(df_success)
    duplicate_upis = total_upis - unique_upis
    
    # ===========================================
    # 1. OVERVIEW 
    # ===========================================
    st.markdown('<div class="section-header">1. Overview</div>', unsafe_allow_html=True)
    
    col1, col2, col3, col4, col5 = st.columns(5)
    
    with col1:
        st.metric("Total Attempts", f"{total_records:,}")
    with col2:
        st.metric("Success Rate", f"{success_rate:.1f}%", delta=f"{success_count:,} passed")
    with col3:
        st.metric("Unique UPIs", f"{unique_upis:,}", delta=f"{duplicate_upis:,} duplicates")
    with col4:
        st.metric("Total Failures", f"{fail_count:,}", delta=f"{(fail_count/total_records*100):.1f}%")
    with col5:
        st.metric("Sites / Proxies", f"{unique_sites} / {unique_proxies}")
    
    # Quick insights row
    col1, col2, col3 = st.columns(3)
    
    efficiency = total_records / unique_upis if unique_upis > 0 else 0
    efficiency_class = 'success-highlight' if efficiency < 3 else 'warning-highlight' if efficiency < 5 else 'danger-highlight'
    
    with col1:
        st.markdown(f"""
        <div class="insight-card {efficiency_class}">
            <strong>Efficiency:</strong> {efficiency:.1f} attempts per unique UPI
        </div>
        """, unsafe_allow_html=True)
    
    with col2:
        top_error = df_failures['error_type'].value_counts().index[0] if fail_count > 0 else "None"
        top_error_pct = (df_failures['error_type'].value_counts().iloc[0] / fail_count * 100) if fail_count > 0 else 0
        st.markdown(f"""
        <div class="insight-card danger-highlight">
            <strong>Top Error:</strong> {top_error} ({top_error_pct:.1f}% of failures)
        </div>
        """, unsafe_allow_html=True)
    
    with col3:
        time_range = f"{df['datetime_ist'].min().strftime('%d %b %H:%M')} - {df['datetime_ist'].max().strftime('%d %b %H:%M')}"
        st.markdown(f"""
        <div class="insight-card">
            <strong>Time Range:</strong> {time_range} IST
        </div>
        """, unsafe_allow_html=True)
    
    st.markdown("---")
    
    # ===========================================
    # 2. PROXY ANALYSIS
    # ===========================================
    st.markdown('<div class="section-header">2. Proxy Analysis</div>', unsafe_allow_html=True)
    
    proxy_stats = df.groupby('proxy').agg(
        total=('is_success', 'count'),
        successes=('is_success', 'sum'),
        unique_upis=('upi_extracted', lambda x: x.dropna().nunique())
    ).reset_index()
    proxy_stats['failures'] = proxy_stats['total'] - proxy_stats['successes']
    proxy_stats['success_rate'] = (proxy_stats['successes'] / proxy_stats['total'] * 100).round(1)
    proxy_stats = proxy_stats.sort_values('success_rate', ascending=False)
    
    col1, col2 = st.columns([2, 1])
    
    with col1:
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        
        fig.add_trace(
            go.Bar(name='Success', x=proxy_stats['proxy'], y=proxy_stats['successes'], marker_color='#2ecc71'),
            secondary_y=False
        )
        fig.add_trace(
            go.Bar(name='Failures', x=proxy_stats['proxy'], y=proxy_stats['failures'], marker_color='#e74c3c'),
            secondary_y=False
        )
        fig.add_trace(
            go.Scatter(name='Success Rate %', x=proxy_stats['proxy'], y=proxy_stats['success_rate'],
                      mode='lines+markers', line=dict(color='#3498db', width=3), marker=dict(size=10)),
            secondary_y=True
        )
        
        fig.update_layout(
            title="Proxy Performance: Success/Failure Count & Success Rate",
            barmode='stack',
            height=350,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(t=80, l=10, r=60, b=0)
        )
        fig.update_yaxes(title_text="Count", secondary_y=False)
        fig.update_yaxes(title_text="Success Rate %", secondary_y=True, range=[0, 100])
        st.plotly_chart(fig, use_container_width=True)
    
    with col2:
        st.markdown("**Proxy Rankings**")
        display_proxy = proxy_stats[['proxy', 'total', 'successes', 'failures', 'success_rate', 'unique_upis']].copy()
        display_proxy.columns = ['Proxy', 'Total', 'Success', 'Fail', 'Rate %', 'Unique UPIs']
        st.dataframe(display_proxy, use_container_width=True, hide_index=True)
        
        best_proxy = proxy_stats.iloc[0]
        worst_proxy = proxy_stats.iloc[-1]
        st.success(f"**Best:** {best_proxy['proxy']} ({best_proxy['success_rate']}%)")
        st.error(f"**Worst:** {worst_proxy['proxy']} ({worst_proxy['success_rate']}%)")
    
    st.markdown("---")
    
    # ===========================================
    # 3. ALL SITES PERFORMANCE
    # ===========================================
    st.markdown('<div class="section-header">3. All Sites Performance</div>', unsafe_allow_html=True)

    # Calculate UPIs from last 1 hour for each site
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    one_hour_ago = (datetime.now(ist_tz) - timedelta(hours=1)).replace(tzinfo=None)
    df_last_hour = df[df['datetime_ist'] >= one_hour_ago].copy()
    upis_last_hour = df_last_hour[df_last_hour['is_success']].groupby('scraper')['upi_extracted'].apply(
        lambda x: x.dropna().nunique()
    ).reset_index(name='upis_last_hour')

    # Calculate comprehensive site stats
    site_stats = df.groupby('scraper').agg(
        total_attempts=('is_success', 'count'),
        total_success=('is_success', 'sum'),
        total_upis=('upi_extracted', lambda x: x.notna().sum()),
        distinct_upis=('upi_extracted', lambda x: x.dropna().nunique())
    ).reset_index()
    site_stats['total_failures'] = site_stats['total_attempts'] - site_stats['total_success']
    site_stats['success_rate'] = (site_stats['total_success'] / site_stats['total_attempts'] * 100).round(1)
    site_stats['duplicate_upis'] = site_stats['total_upis'] - site_stats['distinct_upis']

    # Merge UPIs from last hour
    site_stats = site_stats.merge(upis_last_hour, on='scraper', how='left')
    site_stats['upis_last_hour'] = site_stats['upis_last_hour'].fillna(0).astype(int)
    top_site = site_stats.nlargest(1, 'total_attempts').iloc[0]
    best_site = site_stats.nlargest(1, 'success_rate').iloc[0]
    most_upis = site_stats.nlargest(1, 'distinct_upis').iloc[0]
    avg_success = site_stats['success_rate'].mean()
    
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Most Active Site", top_site['scraper'][:20] + '...' if len(top_site['scraper']) > 20 else top_site['scraper'],
                 delta=f"{top_site['total_attempts']} attempts")
    with col2:
        st.metric("Best Success Rate", best_site['scraper'][:20] + '...' if len(best_site['scraper']) > 20 else best_site['scraper'],
                 delta=f"{best_site['success_rate']}%")
    with col3:
        st.metric("Most Unique UPIs", most_upis['scraper'][:20] + '...' if len(most_upis['scraper']) > 20 else most_upis['scraper'],
                 delta=f"{most_upis['distinct_upis']} UPIs")
    with col4:
        st.metric("Average Success Rate", f"{avg_success:.1f}%",
                 delta=f"{len(site_stats[site_stats['success_rate'] >= avg_success])} sites above avg")
    
    fig = px.treemap(
        site_stats,
        path=['scraper'],
        values='total_attempts',
        color='success_rate',
        color_continuous_scale='RdYlGn',
        range_color=[0, 100],
        custom_data=['total_attempts', 'total_success', 'total_failures', 'success_rate', 'distinct_upis', 'duplicate_upis', 'upis_last_hour']
    )

    fig.update_traces(
        textposition='middle center',
        texttemplate='<b>%{label}</b><br>%{customdata[0]} attempts',
        hovertemplate='<b>%{label}</b><br><br>' +
                      'Total Attempts: %{customdata[0]}<br>' +
                      'Success: %{customdata[1]}<br>' +
                      'Failures: %{customdata[2]}<br>' +
                      'Success Rate: %{customdata[3]:.1f}%<br>' +
                      'Distinct UPIs: %{customdata[4]}<br>' +
                      'Duplicate UPIs: %{customdata[5]}<br>' +
                      'UPIs (Last Hour): %{customdata[6]}<br>' +
                      '<extra></extra>'
    )
    
    fig.update_layout(
        height=700,
        coloraxis_colorbar=dict(
            title="Success<br>Rate %",
            ticksuffix="%"
        ),
        margin=dict(t=10, l=10, r=10, b=10)
    )
    
    st.plotly_chart(fig, use_container_width=True)
    
    st.markdown("---")
    st.markdown("### 🔴 Problem Sites Needing Attention")
    col1, col2 = st.columns(2)
    with col1:
        success_threshold = st.slider("Success Rate Threshold (%)", 0, 100, 50, 5,
                                     help="Show sites with success rate below this threshold")
    with col2:
        min_attempts = st.slider("Minimum Attempts", 1, 20, 3, 1,
                                help="Only show sites with at least this many attempts")
    
    problem_sites = site_stats[
        (site_stats['success_rate'] < success_threshold) & 
        (site_stats['total_attempts'] >= min_attempts)
    ].sort_values('total_failures', ascending=False)
    
    if problem_sites.empty:
        st.success(f"✅ No sites below {success_threshold}% success rate with {min_attempts}+ attempts!")
    else:
        st.warning(f"Found **{len(problem_sites)}** sites below {success_threshold}% success rate with {min_attempts}+ attempts")
        fig = go.Figure()
        
        fig.add_trace(go.Bar(
            name='Failures',
            y=problem_sites['scraper'],
            x=problem_sites['total_failures'],
            orientation='h',
            marker_color='#e74c3c',
            text=problem_sites['total_failures'],
            textposition='inside'
        ))
        
        fig.add_trace(go.Bar(
            name='Success',
            y=problem_sites['scraper'],
            x=problem_sites['total_success'],
            orientation='h',
            marker_color='#95a5a6',
            text=problem_sites['total_success'],
            textposition='inside'
        ))
        
        fig.update_layout(
            title="Problem Sites: Failures vs Success",
            barmode='stack',
            height=max(400, len(problem_sites) * 30),
            yaxis={'categoryorder': 'total descending'},
            showlegend=True,
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
            xaxis_title="Count",
            margin=dict(t=80, l=10, r=10, b=10)
        )
        st.plotly_chart(fig, use_container_width=True)
        st.markdown("**Detailed Problem Sites Report**")
        problem_display = problem_sites[['scraper', 'total_attempts', 'total_success', 'total_failures', 'success_rate', 'distinct_upis']].copy()
        problem_display.columns = ['Site', 'Total Attempts', 'Success', 'Failures', 'Success Rate %', 'Distinct UPIs']
        st.dataframe(
            problem_display.style.background_gradient(subset=['Success Rate %'], cmap='RdYlGn', vmin=0, vmax=100)
                                .background_gradient(subset=['Failures'], cmap='Reds'),
            use_container_width=True,
            hide_index=True,
            height=min(600, len(problem_sites) * 35 + 38)
        )
    
    st.markdown("---")

    # ===========================================
    # 3.5 INACTIVE & ZERO-SUCCESS SITES (LAST 1 HOUR)
    # ===========================================
    st.markdown('<div class="section-header">3.5 Inactive & Zero-Success Sites (Last 1 Hour)</div>', unsafe_allow_html=True)

    # Filter data for last 1 hour
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    one_hour_ago = (datetime.now(ist_tz) - timedelta(hours=1)).replace(tzinfo=None)
    df_last_hour = df[df['datetime_ist'] >= one_hour_ago].copy()

    # Sites with activity in last hour
    sites_with_attempts = set(df_last_hour['scraper'].dropna().unique())

    # Sites with success in last hour
    sites_with_success = set(df_last_hour[df_last_hour['is_success']]['scraper'].dropna().unique())

    # Sites with no attempts in last hour
    sites_no_attempts = working_sites - sites_with_attempts

    # Sites with attempts but zero success in last hour
    sites_zero_success = sites_with_attempts - sites_with_success

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("### 🔴 No Attempts (Last 1 Hour)")
        if sites_no_attempts:
            st.warning(f"**{len(sites_no_attempts)}** sites have not been attempted in the last hour")
            no_attempts_df = pd.DataFrame({'Site': sorted(sites_no_attempts)})
            st.dataframe(no_attempts_df, use_container_width=True, hide_index=True, height=min(400, len(sites_no_attempts) * 35 + 38))
        else:
            st.success("✅ All sites have been attempted in the last hour")

    with col2:
        st.markdown("### ⚠️ Zero Success (Last 1 Hour)")
        if sites_zero_success:
            st.warning(f"**{len(sites_zero_success)}** sites had attempts but zero success")
            # Get attempt counts for these sites
            zero_success_stats = df_last_hour[df_last_hour['scraper'].isin(sites_zero_success)].groupby('scraper').size().reset_index(name='Attempts')
            zero_success_stats = zero_success_stats.sort_values('Attempts', ascending=False)
            zero_success_stats.columns = ['Site', 'Failed Attempts']
            st.dataframe(
                zero_success_stats.style.background_gradient(subset=['Failed Attempts'], cmap='Reds'),
                use_container_width=True, 
                hide_index=True,
                height=min(400, len(sites_zero_success) * 35 + 38)
            )
        else:
            st.success("✅ All attempted sites had at least one success")

    st.markdown("---")
    
    # ===========================================
    # 4. SITE-WISE INSIGHTS (Deep Dive)
    # ===========================================
    st.markdown('<div class="section-header">4. Site-Wise Deep Dive</div>', unsafe_allow_html=True)
    all_sites = sorted(df['scraper'].dropna().unique().tolist())
    selected_site = st.selectbox("Select a site to analyze", all_sites, index=0 if all_sites else None)
    
    if selected_site:
        site_df = df[df['scraper'] == selected_site]
        site_success_df = site_df[site_df['is_success']]
        site_fail_df = site_df[~site_df['is_success']]
        col1, col2, col3, col4, col5 = st.columns(5)
        
        site_total = len(site_df)
        site_success = site_df['is_success'].sum()
        site_fail = site_total - site_success
        site_rate = (site_success / site_total * 100) if site_total > 0 else 0
        site_unique_upis = site_success_df['upi_extracted'].nunique()
        site_dup_upis = len(site_success_df) - site_unique_upis
        
        with col1:
            st.metric("Total Attempts", site_total)
        with col2:
            st.metric("Success Rate", f"{site_rate:.1f}%")
        with col3:
            st.metric("Successes / Failures", f"{site_success} / {site_fail}")
        with col4:
            st.metric("Unique UPIs", site_unique_upis)
        with col5:
            st.metric("Duplicate UPIs", site_dup_upis)
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            site_proxy_stats = site_df.groupby('proxy').agg(
                attempts=('is_success', 'count'),
                success=('is_success', 'sum')
            ).reset_index()
            site_proxy_stats['rate'] = (site_proxy_stats['success'] / site_proxy_stats['attempts'] * 100).round(1)
            site_proxy_stats = site_proxy_stats.sort_values('rate', ascending=False)
            
            fig = px.bar(
                site_proxy_stats,
                x='proxy',
                y='rate',
                color='rate',
                color_continuous_scale='RdYlGn',
                text=site_proxy_stats.apply(lambda x: f"{x['rate']}%", axis=1)
            )
            fig.update_traces(textposition='inside')
            fig.update_layout(height=250, showlegend=False, title="Success Rate by Proxy", margin=dict(t=40, l=10, r=10, b=0))
            st.plotly_chart(fig, use_container_width=True)
        
        with col2:
            # Error distribution for this site
            if len(site_fail_df) > 0:
                site_errors = site_fail_df['error_type'].value_counts()
                fig = px.pie(
                    values=site_errors.values,
                    names=site_errors.index,
                    color_discrete_sequence=px.colors.sequential.Reds_r
                )
                fig.update_layout(height=250, title="Failure Types", margin=dict(t=40, l=10, r=10, b=10))
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.success("No failures for this site!")
        
        with col3:
            if len(site_success_df) > 0:
                site_providers = site_success_df['upi_provider'].value_counts()
                fig = px.pie(
                    values=site_providers.values,
                    names=site_providers.index,
                    color_discrete_sequence=px.colors.sequential.Blues_r
                )
                fig.update_layout(height=250, title="UPI Provider Distribution", margin=dict(t=40, l=10, r=10, b=20))
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.warning("No successful UPI extractions")
        
    st.markdown("---")  
    
    # ===========================================
    # 5. FAILURE ANALYSIS & ERROR BREAKDOWN
    # ===========================================
    st.markdown('<div class="section-header">5. Failure Analysis & Error Breakdown</div>', unsafe_allow_html=True)
    
    if fail_count == 0:
        st.success("✅ No failures to analyze!")
    else:
        failure_counts = df_failures['error_type'].value_counts().reset_index()
        failure_counts.columns = ['Error Type', 'Count']
        failure_counts['Percentage'] = (failure_counts['Count'] / fail_count * 100).round(1)
        failure_counts = failure_counts.sort_values('Count', ascending=True)
        fig = go.Figure()
        colors = []
        max_count = failure_counts['Count'].max()
        for count in failure_counts['Count']:
            intensity = count / max_count
            colors.append(f'rgba(231, 76, 60, {0.3 + 0.7 * intensity})')
        
        fig.add_trace(go.Bar(
            x=failure_counts['Count'],
            y=failure_counts['Error Type'],
            orientation='h',
            marker=dict(
                color=colors,
                line=dict(color='rgba(231, 76, 60, 1)', width=2)
            ),
            text=[f"<b>{count}</b> ({pct}%)" for count, pct in zip(failure_counts['Count'], failure_counts['Percentage'])],
            textposition='outside',
            textfont=dict(size=12, color='black'),
            hovertemplate='<b>%{y}</b><br>Count: %{x}<br><extra></extra>'
        ))
        
        fig.update_layout(
            title="Failure Types Distribution (with color intensity)",
            height=max(300, len(failure_counts) * 40),
            showlegend=False,
            xaxis_title="Number of Failures",
            yaxis_title="",
            margin=dict(t=60, l=10, r=120, b=40),
            plot_bgcolor='rgba(248, 249, 250, 0.5)',
            xaxis=dict(showgrid=True, gridcolor='rgba(0,0,0,0.1)')
        )
        
        st.plotly_chart(fig, use_container_width=True)
        
        st.markdown("---")
        st.markdown("### 📋 Error Breakdown by Site")
        site_error_stats = df_failures.groupby(['scraper', 'error_type']).size().reset_index(name='count')
        all_errors_pivot = site_error_stats.pivot(index='scraper', columns='error_type', values='count').fillna(0).astype(int)
        all_errors_pivot['Total'] = all_errors_pivot.sum(axis=1)
        all_errors_pivot = all_errors_pivot.sort_values('Total', ascending=False)
        all_errors_pivot = all_errors_pivot.reset_index()
        priority_cols = ['scraper', 'Total', 'Scrape Timeout', 'Element Timeout', 'No UPI Extracted', 'Page Timeout']
        other_cols = [col for col in all_errors_pivot.columns if col not in priority_cols]
        final_cols = [col for col in priority_cols if col in all_errors_pivot.columns] + other_cols
        all_errors_pivot = all_errors_pivot[final_cols]
        all_errors_pivot = all_errors_pivot.rename(columns={'scraper': 'Site'})
        st.dataframe(
            all_errors_pivot.style.background_gradient(subset=[col for col in all_errors_pivot.columns if col != 'Site'], cmap='Reds', vmin=0),
            use_container_width=True,
            hide_index=True,
            height=min(400, len(all_errors_pivot) * 35 + 38)
        )
    
    
    # ===========================================
    # 6. UPI ANALYSIS - Unique vs Duplicate
    # ===========================================
    st.markdown('<div class="section-header">6. UPI Analysis: Unique vs Duplicate</div>', unsafe_allow_html=True)
    
    if df_success.empty:
        st.warning("No successful UPI extractions to analyze")
    else:
        upi_site_stats = df_success.groupby('scraper').agg(
            total_upis=('upi_extracted', 'count'),
            unique_upis=('upi_extracted', 'nunique')
        ).reset_index()
        upi_site_stats['duplicate_upis'] = upi_site_stats['total_upis'] - upi_site_stats['unique_upis']
        upi_site_stats['dup_rate'] = (upi_site_stats['duplicate_upis'] / upi_site_stats['total_upis'] * 100).round(1)
        
        # Add sort options
        col_filter, col_count = st.columns([3, 1])
        with col_filter:
            sort_option = st.selectbox(
                "Sort by",
                ["Total UPIs", "Duplication %", "Unique UPIs", "Duplicate Count"],
                help="Choose how to rank and display sites"
            )
        with col_count:
            top_n = st.number_input("Show top", min_value=5, max_value=20, value=12, step=1)
        
        # Get top sites based on selected sort
        if sort_option == "Total UPIs":
            top_sites = upi_site_stats.nlargest(top_n, 'total_upis')
            chart_title = f"Top {top_n} Sites by Total UPIs"
        elif sort_option == "Duplication %":
            # Filter sites with at least 5 UPIs to avoid skewed percentages
            filtered_stats = upi_site_stats[upi_site_stats['total_upis'] >= 5]
            top_sites = filtered_stats.nlargest(top_n, 'dup_rate')
            chart_title = f"Top {top_n} Sites by Duplication Rate"
            if len(filtered_stats) < len(upi_site_stats):
                st.info(f"📊 Showing sites with ≥5 UPIs to avoid skewed percentages ({len(filtered_stats)} sites)")
        elif sort_option == "Unique UPIs":
            top_sites = upi_site_stats.nlargest(top_n, 'unique_upis')
            chart_title = f"Top {top_n} Sites by Unique UPIs"
        else:  # Duplicate Count
            filtered_stats = upi_site_stats[upi_site_stats['duplicate_upis'] > 0]
            top_sites = filtered_stats.nlargest(top_n, 'duplicate_upis')
            chart_title = f"Top {top_n} Sites by Duplicate Count"
        
        if top_sites.empty:
            st.warning("No sites match the selected criteria")
        else:
            fig = go.Figure()
            
            # Unique UPIs (Green) - base of the stack
            fig.add_trace(go.Bar(
                name='Unique UPIs',
                y=top_sites['scraper'],
                x=top_sites['unique_upis'],
                orientation='h',
                marker=dict(
                    color='#27ae60',
                    line=dict(color='#229954', width=0.5)
                ),
                text=top_sites['unique_upis'].apply(lambda x: f'{int(x)}'),
                textposition='inside',
                textfont=dict(color='white', size=11, family='Arial Black'),
                hovertemplate='<b>%{y}</b><br>Unique UPIs: %{x:,}<extra></extra>'
            ))
            
            # Duplicate UPIs (Red) - stacked on top
            fig.add_trace(go.Bar(
                name='Duplicate UPIs',
                y=top_sites['scraper'],
                x=top_sites['duplicate_upis'],
                orientation='h',
                marker=dict(
                    color='#e74c3c',
                    line=dict(color='#c0392b', width=0.5)
                ),
                text=top_sites['duplicate_upis'].apply(lambda x: f'{int(x)}' if x > 0 else ''),
                textposition='inside',
                textfont=dict(color='white', size=11, family='Arial Black'),
                hovertemplate='<b>%{y}</b><br>Duplicate UPIs: %{x:,}<extra></extra>'
            ))
            
            # Add total count and dup% annotations
            annotations = []
            for idx, row in top_sites.iterrows():
                # Total count annotation
                annotations.append(
                    dict(
                        x=row['total_upis'],
                        y=row['scraper'],
                        text=f"  <b>{int(row['total_upis'])}</b>  ",
                        showarrow=False,
                        xanchor='left',
                        font=dict(size=11, color='#2c3e50', family='Arial'),
                        bgcolor='rgba(255, 255, 255, 0.8)',
                        borderpad=2
                    )
                )
                # Dup percentage badge
                if row['dup_rate'] > 0:
                    annotations.append(
                        dict(
                            x=row['total_upis'],
                            y=row['scraper'],
                            text=f"  {row['dup_rate']}%  ",
                            showarrow=False,
                            xanchor='left',
                            xshift=60,
                            font=dict(size=10, color='white', family='Arial Black'),
                            bgcolor='#e74c3c',
                            borderpad=2,
                            bordercolor='#c0392b',
                            borderwidth=1
                        )
                    )
            
            fig.update_layout(
                barmode='stack',
                height=max(500, len(top_sites) * 45),
                title=dict(
                    text=f"<b>UPI Distribution: Unique vs Duplicate ({chart_title})</b>",
                    font=dict(size=16, color='#2c3e50')
                ),
                xaxis=dict(
                    title=dict(
                        text="<b>Number of UPIs</b>",
                        font=dict(size=13)
                    ),
                    showgrid=True,
                    gridcolor='#ecf0f1'
                ),
                yaxis=dict(
                    categoryorder='total ascending',
                    tickfont=dict(size=10)
                ),
                legend=dict(
                    orientation="h",
                    yanchor="bottom",
                    y=1.01,
                    xanchor="right",
                    x=1,
                    font=dict(size=12),
                    bgcolor='rgba(255, 255, 255, 0.8)',
                    bordercolor='#bdc3c7',
                    borderwidth=1
                ),
                annotations=annotations,
                margin=dict(t=80, l=10, r=120, b=50),
                plot_bgcolor='white',
                paper_bgcolor='white'
            )
            
            st.plotly_chart(fig, use_container_width=True)
    
    st.markdown("---")
    
    # ===========================================
    # 7. SUCCESS VS FAILURE OVER TIME
    # ===========================================
    st.markdown('<div class="section-header">7. Success vs Failure Over Time</div>', unsafe_allow_html=True)
    
    bucket_options = {'5 min': '5min', '10 min': '10min', '15 min': '15min', '30 min': '30min', '1 hour': '1h'}
    bucket_label = st.selectbox("Time bucket", list(bucket_options.keys()), index=4)
    bucket = bucket_options[bucket_label]
    
    df['time_bucket'] = df['datetime_ist'].dt.floor(bucket)
    
    time_stats = df.groupby('time_bucket').agg(
        total=('is_success', 'count'),
        successes=('is_success', 'sum')
    ).reset_index()
    time_stats['failures'] = time_stats['total'] - time_stats['successes']
    time_stats['success_rate'] = (time_stats['successes'] / time_stats['total'] * 100).round(1)
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=time_stats['time_bucket'],
        y=time_stats['successes'],
        mode='lines+markers',
        name='Success',
        line=dict(color='#2ecc71', width=2),
        fill='tozeroy',
        fillcolor='rgba(46, 204, 113, 0.2)'
    ))
    fig.add_trace(go.Scatter(
        x=time_stats['time_bucket'],
        y=time_stats['failures'],
        mode='lines+markers',
        name='Failure',
        line=dict(color='#e74c3c', width=2),
        fill='tozeroy',
        fillcolor='rgba(231, 76, 60, 0.2)'
    ))
    fig.update_layout(
        xaxis_title="Time (IST)",
        yaxis_title="Count",
        height=370,
        hovermode='x unified',
        margin=dict(t=20, l=10, r=10, b=10)
    )
    st.plotly_chart(fig, use_container_width=True)
    col1, col2, col3, col4 = st.columns(4)
    
    # Exclude last bucket from stats as it's not fully updated
    time_stats = time_stats.iloc[:-1] if len(time_stats) > 1 else time_stats
    
    peak_idx = time_stats['success_rate'].idxmax()
    low_idx = time_stats['success_rate'].idxmin()
    avg_rate = time_stats['success_rate'].mean()
    
    with col1:
        st.metric("Peak Success", f"{time_stats.loc[peak_idx, 'success_rate']:.1f}%",
                 delta=f"at {time_stats.loc[peak_idx, 'time_bucket'].strftime('%H:%M')}")
    with col2:
        st.metric("Lowest Success", f"{time_stats.loc[low_idx, 'success_rate']:.1f}%",
                 delta=f"at {time_stats.loc[low_idx, 'time_bucket'].strftime('%H:%M')}")
    with col3:
        st.metric("Avg Success Rate", f"{avg_rate:.1f}%")
    with col4:
        volatility = time_stats['success_rate'].std()
        st.metric("Rate Volatility", f"{volatility:.1f}%", 
                 delta="Stable" if volatility < 10 else "Unstable")
        
    st.markdown("---")
    st.markdown("### Export Data")
    col1, col2 = st.columns(2)
    
    with col1:
        if len(df_success) > 0:
            csv_upi = df_success[['date', 'datetime_ist', 'scraper', 'proxy', 'upi_extracted', 'upi_provider']].to_csv(index=False)
            st.download_button("Download Full Data", csv_upi, f"pench_upis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv", "text/csv")
    
    with col2:
        csv_sites = site_stats.to_csv(index=False)
        st.download_button("Download Site Stats", csv_sites, f"pench_sites_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv", "text/csv")


def main():
    """Entry point for the scrape-dashboard command."""
    # Get the path to this file
    dashboard_path = __file__
    
    # Launch Streamlit with this script
    print("\n📊 Launching Pench Alerts Dashboard...")
    print("   Dashboard will open in your browser automatically")
    print("   Press Ctrl+C to stop\n")
    
    subprocess.run([sys.executable, "-m", "streamlit", "run", dashboard_path])


if __name__ == "__main__":
    run_app()
