import datetime
import html
from html import escape
from pathlib import Path
from typing import List, Optional

from praktika.result import Result
from praktika.utils import Utils
from praktika.s3 import S3
from praktika.settings import Settings
from praktika.environment import Environment


class HtmlGenerator:
    @classmethod
    def escape(cls, text: str) -> str:
        return html.escape(text)

    @classmethod
    def format_duration(cls, seconds: Optional[float]) -> str:
        if seconds is None:
            return ""
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        h = int(h)
        m = int(m)
        ms = int(str(int(s * 10))[-1])
        s = int(s)
        parts = [f"{h}h", f" {m}m", f" {s}.{ms}s"]
        return "".join(parts)

    @classmethod
    def generate_html(cls, result: Result) -> str:
        def get_status_color(status):
            if status == Result.Status.SUCCESS:
                color = "#28a745"
            elif status == Result.Status.FAILED:
                color = "#dc3545"
            elif status == Result.Status.ERROR:
                color = "#b00020"
            elif status == Result.Status.PENDING:
                color = "#ffc107"
            elif status == Result.Status.RUNNING:
                color = "#007bff"
            elif status == Result.Status.SKIPPED:
                color = "#6c757d"
            else:
                print(f"WARNING: Status unknown [{status}] for job [{result.name}]")
                color = "#6c757d"
            return color

        def generate_urls(urls: Optional[List[str]]) -> str:
            if not urls:
                return ""
            buttons = []
            for url in urls:
                url_name = url.split("/")[-1] if "/" in url else url
                buttons.append(
                    f'<a href="{escape(url)}" class="button">{escape(url_name)}</a>'
                )
            return " ".join(buttons)

        def generate_table(results: Optional[List[Result]]) -> str:
            if not results:
                return ""

            headers = ["Name", "Status"]
            has_start_time = False
            has_urls = False
            has_duration = False
            has_files = False
            for res in results:
                if res.start_time:
                    has_start_time = True
                if res.duration:
                    has_duration = True
                if res.urls:
                    has_urls = True
                if res.files:
                    has_files = True
            if has_start_time:
                headers.append("Start Time")
            if has_duration:
                headers.append("Duration")
            if has_urls:
                headers.append("Urls")
            if has_files:
                headers.append("Files")

            table_headers = (
                "".join(f"<th>{escape(h.replace('_', ' '))}</th>" for h in headers)
                + "\n"
            )

            table_rows = []
            for res in results:
                urls = generate_urls(res.urls)
                files = generate_urls(
                    res.files
                )  # Reusing the URL function for files if any

                name = escape(res.name)
                if res.html_link:
                    name = f'<a href="{escape(res.html_link)}">{name}</a>'

                info_content = f"<pre>{escape(res.info)}</pre>" if res.info else ""
                row = f'<tr class="expandable">' if res.info else "<tr>"
                row += f"<td>{name}</td>"
                row += f'<td class="status-cell" style="font-weight: bold;color: {get_status_color(res.status)};">{escape(res.status)}'
                if info_content:
                    row += '<span class="expand-indicator">▸</span>'
                row += "</td>"
                if has_start_time:
                    row += f"<td>{escape(Utils.timestamp_to_str(res.start_time)) if res.start_time else ''}</td>"
                if has_duration:
                    row += f"<td>{escape(cls.format_duration(res.duration)) if res.duration else ''}</td>"
                if has_urls:
                    row += f"<td>{urls}</td>"
                if has_files:
                    row += f"<td>{files}</td>"
                row += f"</tr>"
                if info_content:
                    row += f'<tr class="expandable-content"><td colspan="6">{info_content}</td></tr>'
                table_rows.append(row + "\n")

            return f"<table><tr>{table_headers}</tr>{''.join(table_rows)}</table>"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                :root {{
                    --color: white;
                    --background: hsl(210deg, 50%, 10%) linear-gradient(180deg, hsl(210deg, 50%, 15%), hsl(210deg, 50%, 5%));
                    --td-background: hsl(210deg, 50%, 20%);
                    --th-background: hsl(210deg, 50%, 25%);
                    --link-color: #4CC9F0;
                    --link-hover-color: #4895EF;
                    --menu-background: hsl(210deg, 50%, 30%);
                    --menu-hover-background: hsl(210deg, 50%, 35%);
                    --menu-hover-color: white;
                    --text-gradient: linear-gradient(90deg, #4CC9F0, #F72585);
                    --shadow-intensity: 0.2;
                    --tr-hover-filter: brightness(110%);
                    --table-border-color: hsl(210deg, 50%, 30%);
                    --table-header-font-size: 1.2rem;
                    --table-body-font-size: 1rem;
                    --table-padding: 8px 15px;
                }}
    
                [data-theme="light"] {{
                    --color: black;
                    --background: hsl(210deg, 50%, 95%) linear-gradient(180deg, #FFF, #EEE);
                    --td-background: white;
                    --th-background: #DDD;
                    --link-color: #0077B6;
                    --link-hover-color: #023E8A;
                    --menu-background: white;
                    --menu-hover-background: #EEE;
                    --menu-hover-color: #023E8A;
                    --text-gradient: linear-gradient(90deg, black, black);
                    --shadow-intensity: 0.1;
                    --tr-hover-filter: brightness(95%);
                    --table-border-color: #CCC;
                }}
    
                .gradient {{
                    background-image: var(--text-gradient);
                    background-size: 100% 100%;
                    background-repeat: repeat;
                    -webkit-background-clip: text;
                    -webkit-text-fill-color: transparent;
                    color: black;
                }}
                html {{ min-height: 100%; font-family: "Arial", sans-serif; background: var(--background); color: var(--color); }}
                h1 {{ margin-left: 20px; font-size: 2rem; }}
                th, td {{
                    padding: var(--table-padding);
                    text-align: left;
                    vertical-align: middle;
                    line-height: 1.5;
                    border: 1px solid var(--table-border-color);
                    font-size: var(--table-body-font-size);
                }}
                td {{ background: var(--td-background); }}
                th {{
                    background: var(--th-background);
                    white-space: nowrap;
                    font-size: var(--table-header-font-size);
                    font-weight: bold;
                }}
                a {{ color: var(--link-color); text-decoration: none; }}
                a:hover, a:active {{ color: var(--link-hover-color); text-decoration: none; }}
                table {{
                    box-shadow: 0 8px 25px -5px rgba(0, 0, 0, var(--shadow-intensity));
                    border-collapse: collapse;
                    border-spacing: 0;
                    width: 100%;
                    margin: 20px auto;
                    max-width: 1200px;
                }}
                p.links a {{
                    padding: 10px 15px;
                    margin: 5px;
                    background: var(--menu-background);
                    line-height: 2;
                    white-space: nowrap;
                    border-radius: 5px;
                    display: inline-block;
                    transition: background 0.3s, color 0.3s;
                }}
                p.links a:hover {{ background: var(--menu-hover-background); color: var(--menu-hover-color); }}
                th {{ cursor: pointer; }}
                tr:hover {{ filter: var(--tr-hover-filter); }}
                .expandable {{ cursor: pointer; }}
                .expandable-content {{ display: none; }}
                pre {{ white-space: pre-wrap; font-size: 0.9rem; background: var(--td-background); padding: 10px; border-radius: 5px; }}
                #fish {{ display: none; float: right; position: relative; top: -20em; right: 2vw; margin-bottom: -20em; width: 30vw; filter: brightness(7%); z-index: -1; }}
    
                .themes {{
                    float: right;
                    font-size: 1.5rem;
                    margin-bottom: 1rem;
                }}
    
                #toggle-theme {{
                    padding-right: 0.5rem;
                    user-select: none;
                    cursor: pointer;
                    display: inline-block;
                    transform: translate(1px, 1px);
                    filter: brightness(125%);
                    transition: filter 0.3s;
                }}
                #toggle-autoreload {{
                    padding-right: 0.5rem;
                    user-select: none;
                    cursor: pointer;
                    display: inline-block;
                    transform: translate(1px, 1px);
                    filter: brightness(125%);
                    transition: filter 0.3s;
                }}
                .status-cell {{
                    display: flex;
                    justify-content: space-between;
                    align-items: center; /* Vertically align the text and indicator */
                    padding-right: 15px; /* Add some padding to the right for better spacing */
                }}

                .expand-indicator {{
                    font-size: 0.8em;
                    margin-left: 5px;
                    cursor: pointer;
                    color: #888;  /* Subtle color to match the theme */
                    transition: transform 0.2s ease;
                }}
            </style>
            <title>{escape(result.name)}</title>
        </head>
        <body>
        <div class="main">
            <span class="nowrap themes">
                <span id="toggle-autoreload">🔄</span>
            </span>
            <span class="nowrap themes">
                <span id="toggle-theme">🔆</span>
            </span>
            <h1><span class="gradient">{escape(result.name)}</span></h1>
            <p>{escape(result.info)}</p>
            <p>Start Time: {escape(Utils.timestamp_to_str(result.start_time))}</p>
            <p>Duration: {escape(cls.format_duration(result.duration)) if result.status not in (Result.Status.PENDING, Result.Status.RUNNING) else '<span id="dynamic-duration"></span>'}</p>
            <p>Status: <span id="status" style="color: {get_status_color(result.status)};"> {escape(result.status)}</span></p>
            <p class="links">
                {generate_urls(result.urls)}
            </p>
            {generate_table(result.results)}
            <img id="fish" src="https://presentations.clickhouse.com/images/fish.png" />
            <script type="text/javascript">
                const getCellValue = (tr, idx) => {{
                    var classes = tr.classList;
                    var elem = tr;
                    if (classes.contains("expandable-content") || classes.contains("expandable-content.open"))
                        elem = tr.previousElementSibling;
                    return elem.children[idx].innerText || elem.children[idx].textContent;
                }}
    
                const comparer = (idx, asc) => (a, b) => ((v1, v2) =>
                    v1 !== '' && v2 !== '' && !isNaN(v1) && !isNaN(v2) ? v1 - v2 : v1.toString().localeCompare(v2)
                )(getCellValue(asc ? a : b, idx), getCellValue(asc ? b : a, idx));
    
                document.querySelectorAll('th').forEach(th => th.addEventListener('click', (() => {{
                    const table = th.closest('table');
                    Array.from(table.querySelectorAll('tr:nth-child(n+2)'))
                        .sort(comparer(Array.from(th.parentNode.children).indexOf(th), this.asc = !this.asc))
                        .forEach(tr => table.appendChild(tr) );
                }})));
    
                Array.from(document.getElementsByClassName("status-cell")).forEach(td => td.addEventListener('click', function() {{
                    var content = this.parentElement.nextElementSibling;
                    content.classList.toggle("expandable-content");
                    // TODO make indicator rotating
                    // content.classList.toggle("open");  // Add or remove the open class
                    // this.querySelector('.expand-indicator').classList.toggle('expanded');  // Toggle the indicator's rotation
                }}));

                let theme = 'dark';
    
                function setTheme(new_theme) {{
                    theme = new_theme;
                    document.documentElement.setAttribute('data-theme', theme);
                    window.localStorage.setItem('theme', theme);
                    drawFish();
                }}
    
                function drawFish() {{
                    document.getElementById('fish').style.display = (document.body.clientHeight > 3000 && theme == 'dark') ? 'block' : 'none';
                }}
    
                document.getElementById('toggle-theme').addEventListener('click', () => {{
                    theme = theme === 'dark' ? 'light' : 'dark';
                    setTheme(theme);
                }});
    
                let saved_theme = window.localStorage.getItem('theme');
                if (saved_theme && saved_theme !== theme) {{
                    setTheme(saved_theme);
                }}
    
                drawFish();
                
                function calculateDuration(startTime) {{
                    let startDate = new Date(startTime);
                    let now = new Date();
                    let diffSeconds = Math.floor((now - startDate) / 1000);
                    let hours = Math.floor(diffSeconds / 3600);
                    let minutes = Math.floor((diffSeconds % 3600) / 60);
                    let seconds = diffSeconds % 60;
                    return `${{hours}}h ${{minutes}}m ${{seconds}}s`;
                }}

                function updateDuration() {{
                    const durationElement = document.getElementById('dynamic-duration');
                    const startTime = {int(result.start_time or 0) * 1000};  // Convert start_time to milliseconds
                    durationElement.innerText = calculateDuration(startTime);
                }}
    
                window.onload = function() {{
                    if ("{result.status}" === "pending") {{
                        updateDuration();
                        setInterval(updateDuration, 1000);  // Update every second
                    }}
                }};
                // Auto-reload functionality
                var status = document.getElementById('status').textContent.trim();
                var autoReloadEnabled = false;  // Auto-reload is initially disabled
                var reloadInterval;

                function startAutoReload() {{
                    reloadInterval = setInterval(function() {{
                        location.reload();
                    }}, 60000); // Reload every 60 seconds
                }}

                function stopAutoReload() {{
                    clearInterval(reloadInterval);
                }}
        
                function toggleAutoReload() {{
                    autoReloadEnabled = !autoReloadEnabled;
                    if (autoReloadEnabled && (status === 'pending' || status === 'running')) {{
                        startAutoReload();
                        document.getElementById('toggle-autoreload').textContent = "⏹️";
                    }} else {{
                        stopAutoReload();
                        document.getElementById('toggle-autoreload').textContent = "🔄";
                    }}
                }}

                // Add event listener to the toggle emoji
                document.getElementById('toggle-autoreload').addEventListener('click', toggleAutoReload);

                // Automatically enable auto-reload if the status is pending or running
                if (status === 'pending' || status === 'running') {{
                    toggleAutoReload();
                }}
        </script>
        </div>
        </body>
        </html>
        """

        return html_content

    @classmethod
    def dump_html(cls, name, content, upload_to_s3: bool) -> str:
        html_file_path = f"{Settings.RESULTS_DIR}/{Utils.normalize_string(name)}.html"
        with open(html_file_path, "w", encoding="utf8") as f:
            f.write(content)
        if upload_to_s3:
            s3_path = f"{Settings.HTML_S3_PATH}/{S3.get_prefix(pr_number=Environment.PR_NUMBER, branch=Environment.BRANCH, sha=Environment.SHA)}"
            html_link = S3.copy_file_to_s3(s3_path=s3_path, local_path=html_file_path)
            return html_link

        return f"file://{Path(html_file_path).absolute()}"

    @classmethod
    def upload_file_to_s3(
        cls, local_file_path, upload_to_s3: bool, text: bool = False
    ) -> str:
        if upload_to_s3:
            s3_path = f"{Settings.HTML_S3_PATH}/{S3.get_prefix(pr_number=Environment.PR_NUMBER, branch=Environment.BRANCH, sha=Environment.SHA)}"
            html_link = S3.copy_file_to_s3(
                s3_path=s3_path, local_path=local_file_path, text=text
            )
            return html_link
        return f"file://{Path(local_file_path).absolute()}"

    @classmethod
    def generate_recursive(
        cls, result: Result, upload_to_s3: bool, changed_items: List[Result] = None
    ) -> Result:
        changed_result_names = [item.name for item in changed_items or []]
        for result_ in result.results or []:
            if changed_result_names and result_.name not in changed_result_names:
                print(
                    f"Result for [{result_.name}] has not been changed - skip updating html"
                )
                continue
            if result_.results:
                if result_.html_link:
                    print(
                        f"Result for [{result_.name}] has own html_link [{result_.html_link}] - report won't be generated"
                    )
                    continue
                _ = cls.generate_recursive(result_, upload_to_s3=upload_to_s3)
                print(
                    f"Html for sub Result [{result_.name}] updated, link [{result_.html_link}]"
                )

        if result.files:
            if not result.urls:
                result.urls = []
            for file in result.files:
                if not Path(file).is_file():
                    print(
                        f"ERROR: Invalid file [{file}] in [{result.name}] @Result - skip upload"
                    )
                    result.info += f"\nWARNING: Result file [{file}] was not found"
                    file_link = cls.upload_file_to_s3(file, upload_to_s3=False)
                else:
                    is_text = False
                    for text_file_suffix in Settings.TEXT_CONTENT_EXTENSIONS:
                        if file.endswith(text_file_suffix):
                            print(
                                f"File [{file}] matches Settings.TEXT_CONTENT_EXTENSIONS [{Settings.TEXT_CONTENT_EXTENSIONS}] - add text attribute for s3 object"
                            )
                            is_text = True
                            break
                    file_link = cls.upload_file_to_s3(
                        file, upload_to_s3=upload_to_s3, text=is_text
                    )
                result.urls.append(file_link)
            print(
                f"Job files [{result.files}] uploaded to s3 [{result.urls[-len(result.files):]}] - clean files list"
            )
            result.files = []

        print(f"Generating HTML for result [{result}]")
        html_content = cls.generate_html(result)
        html_link = cls.dump_html(result.name, html_content, upload_to_s3)
        result.html_link = html_link
        return result


if __name__ == "__main__":
    sample_result = Result(
        status="success",
        name="Job A",
        start_time=datetime.datetime.utcnow().timestamp(),
        duration=3600.5,
        results=[
            Result(
                status="success",
                name="Job 1",
                start_time=datetime.datetime.utcnow().timestamp(),
                duration=600,
                results=[
                    Result(
                        status="success",
                        name="Test A",
                        start_time=datetime.datetime.utcnow().timestamp(),
                        duration=10,
                        results=None,
                        files=["file1.txt", "file2.log"],
                        urls=["http://example.com/report"],
                    ),
                    Result(
                        status="failure",
                        name="Test B",
                        start_time=datetime.datetime.utcnow().timestamp(),
                        duration=20,
                        results=None,
                        files=["error.log"],
                        urls=["http://example.com/error"],
                    ),
                ],
                info="foobar",
            ),
        ],
        files=["lsls"],
        urls=["http://example.com/job"],
    )

    print(HtmlGenerator.generate_recursive(sample_result, upload_to_s3=False))
