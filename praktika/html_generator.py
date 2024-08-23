import dataclasses
import datetime
import json
import os
from html import escape
from pathlib import Path
from typing import List

from praktika.result import Result
from praktika.utils import Utils
from praktika.s3 import S3
from praktika.settings import Settings, Environment


class HtmlGenerator:
    @dataclasses.dataclass
    class HtmlResult:
        result: Result
        html_results: List["HtmlGenerator.HtmlResult"]
        html_file: str = ""
        html_link: str = ""

    class Templates:
        ### BEST FRONTEND PRACTICES BELOW
        HEAD_HTML_TEMPLATE = """
        <!DOCTYPE html>
        <html>
        <head>
          <style>
        
        :root {{
            --color: white;
            --background: hsl(190deg, 90%, 5%) linear-gradient(180deg, hsl(190deg, 90%, 10%), hsl(190deg, 90%, 0%));
            --td-background: hsl(190deg, 90%, 15%);
            --th-background: hsl(180deg, 90%, 15%);
            --link-color: #FF5;
            --link-hover-color: #F40;
            --menu-background: hsl(190deg, 90%, 20%);
            --menu-hover-background: hsl(190deg, 100%, 50%);
            --menu-hover-color: black;
            --text-gradient: linear-gradient(90deg, #8F8, #F88);
            --shadow-intensity: 1;
            --tr-hover-filter: brightness(120%);
            --table-border-color: black;
        }}
        
        [data-theme="light"] {{
            --color: black;
            --background: hsl(190deg, 90%, 90%) linear-gradient(180deg, #EEE, #DEE);
            --td-background: white;
            --th-background: #EEE;
            --link-color: #08F;
            --link-hover-color: #F40;
            --menu-background: white;
            --menu-hover-background: white;
            --menu-hover-color: #F40;
            --text-gradient: linear-gradient(90deg, black, black);
            --shadow-intensity: 0.1;
            --tr-hover-filter: brightness(95%);
            --table-border-color: #DDD;
        }}
        
        .gradient {{
            background-image: var(--text-gradient);
            background-size: 100% 100%;
            background-repeat: repeat;
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            /* Optional: add fallback color for non-WebKit browsers */
            color: black; /* Fallback color */
        }}
        html {{ min-height: 100%; font-family: "DejaVu Sans", "Noto Sans", Arial, sans-serif; background: var(--background); color: var(--color); }}
        h1 {{ margin-left: 10px; }}
        th, td {{ padding: 5px 10px 5px 10px; text-align: left; vertical-align: top; line-height: 1.5; border: 1px solid var(--table-border-color); }}
        td {{ background: var(--td-background); }}
        th {{ background: var(--th-background); white-space: nowrap; }}
        a {{ color: var(--link-color); text-decoration: none; }}
        a:hover, a:active {{ color: var(--link-hover-color); text-decoration: none; }}
        table {{ box-shadow: 0 8px 25px -5px rgba(0, 0, 0, var(--shadow-intensity)); border-collapse: collapse; border-spacing: 0; }}
        p.links a {{ padding: 5px; margin: 3px; background: var(--menu-background); line-height: 2.5; white-space: nowrap; box-shadow: 0 8px 25px -5px rgba(0, 0, 0, var(--shadow-intensity)); }}
        p.links a:hover {{ background: var(--menu-hover-background); color: var(--menu-hover-color); }}
        th {{ cursor: pointer; }}
        tr:hover {{ filter: var(--tr-hover-filter); }}
        .expandable {{ cursor: pointer; }}
        .expandable-content {{ display: none; }}
        pre {{ white-space: pre-wrap; }}
        #fish {{ display: none; float: right; position: relative; top: -20em; right: 2vw; margin-bottom: -20em; width: 30vw; filter: brightness(7%); z-index: -1; }}
        
        .themes {{
            float: right;
            font-size: 20pt;
            margin-bottom: 1rem;
        }}
        
        #toggle-dark, #toggle-light {{
            padding-right: 0.5rem;
            user-select: none;
            cursor: pointer;
        }}
        
        #toggle-dark:hover, #toggle-light:hover {{
            display: inline-block;
            transform: translate(1px, 1px);
            filter: brightness(125%);
        }}
        
          </style>
          <title>{title}</title>
        </head>
        <body>
        <div class="main">
        <span class="nowrap themes"><span id="toggle-dark">🌚</span><span id="toggle-light">🌞</span></span>
        <h1><span class="gradient">{header}</span></h1>
        """

        FOOTER_HTML_TEMPLATE = """<img id="fish" src="https://presentations.clickhouse.com/images/fish.png" />
        <script type="text/javascript">
            /// Straight from https://stackoverflow.com/questions/14267781/sorting-html-table-with-javascript
        
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
        
            Array.from(document.getElementsByClassName("expandable")).forEach(tr => tr.addEventListener('click', function() {{
                var content = this.nextElementSibling;
                content.classList.toggle("expandable-content");
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
        
            document.getElementById('toggle-light').addEventListener('click', e => setTheme('light'));
            document.getElementById('toggle-dark').addEventListener('click', e => setTheme('dark'));
        
            let new_theme = window.localStorage.getItem('theme');
            if (new_theme && new_theme != theme) {{
                setTheme(new_theme);
            }}
        
            drawFish();
        </script>
        </div>
        </body>
        </html>
        """

        HTML_BASE_TEST_TEMPLATE = (
            f"{HEAD_HTML_TEMPLATE}"
            """<p class="links">
        {additional_urls}
        </p>
        {results}
        """
            f"{FOOTER_HTML_TEMPLATE}"
        )

        HTML_TEST_PART = """
        <table>
        <tr>
        {headers}
        </tr>
        {rows}
        </table>
        """

    class ColorTheme:
        def __init__(self, green, red, blue):
            self.green, self.red, self.blue = green, red, blue

        class Color:
            yellow = "#FFB400"
            red = "#F00"
            green = "#0A0"
            blue = "#00B4FF"

        @classmethod
        def default(cls):
            return cls(cls.Color.green, cls.Color.red, cls.Color.yellow)

    @classmethod
    def _format_header(cls, header) -> str:
        return header

    @classmethod
    def _get_status_style(cls, status: str, colortheme) -> str:
        style = "font-weight: bold;"
        if status == Result.Status.SUCCESS:
            style += f"color: {colortheme.green};"
        elif status in (Result.Status.FAILED, Result.Status.ERROR):
            style += f"color: {colortheme.red};"
        else:
            style += f"color: {colortheme.blue};"
        return style

    @staticmethod
    def _get_html_url_name(url):
        base_name = ""
        if isinstance(url, str):
            base_name = os.path.basename(url)
        if isinstance(url, tuple):
            base_name = url[1]

        if "?" in base_name:
            base_name = base_name.split("?")[0]

        if base_name is not None:
            return base_name.replace("%2B", "+").replace("%20", " ")
        return None

    @classmethod
    def _get_html_url(cls, url):
        href = None
        name = None
        if isinstance(url, str):
            href, name = url, cls._get_html_url_name(url)
        if isinstance(url, tuple):
            href, name = url[0], cls._get_html_url_name(url)
        if href and name:
            return f'<a href="{href}">{cls._get_html_url_name(url)}</a>'
        return ""

    @classmethod
    def _format_duration(cls, seconds):
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        sec = seconds % 60

        if isinstance(sec, int):
            sec_str = f"{sec}s"
        else:
            sec_str = f"{sec:.2f}s"

        duration_str = ""
        if hours > 0:
            duration_str += f"{hours}h"
        if minutes > 0 or hours > 0:
            duration_str += f"{minutes}m"
        duration_str += sec_str

        return duration_str

    @classmethod
    def generate_recursive(
        cls, result: Result, upload_to_s3: bool
    ) -> "HtmlGenerator.HtmlResult":
        status_colors = cls.ColorTheme.default()

        html_results = []

        num_fails = 0
        rows_part = []
        for result_ in result.results or []:
            sub_html_link = ""
            if result_.results:
                # result_ has sub results -> generate and upload sub html -> get the link
                # WARNING! recursion!
                sub_html_result = cls.generate_recursive(
                    result_, upload_to_s3=upload_to_s3
                )
                s3_path = f"{Settings.HTML_S3_PATH}/{S3.get_prefix(pr_number=Environment.EventInfo.PR_NUMBER, branch=Environment.BRANCH, sha=Environment.EventInfo.REF_SHA)}"
                if upload_to_s3:
                    sub_html_link = S3.copy_file_to_s3(
                        s3_path=s3_path, local_path=sub_html_result.html_file
                    )
                sub_html_result.html_link = sub_html_link
                html_results.append(sub_html_result)
            colspan = 0
            row = []
            if result_.info is not None:
                row.append('<tr class="expandable">')
            else:
                row.append("<tr>")
            # TODO: make name clickable and attach report link to name if any
            row.append(f"<td>{result_.name}</td>")
            colspan += 1
            style = cls._get_status_style(result_.status, colortheme=status_colors)

            # Allow to quickly scroll to the first failure.
            fail_id = ""
            has_error = result_.status in ("FAIL", "NOT_FAILED")
            if has_error:
                num_fails = num_fails + 1
                fail_id = f'id="fail{num_fails}" '

            row.append(f'<td {fail_id}style="{style}">{result_.status}</td>')
            colspan += 1

            if result_.start_time:
                row.append(f"<td>{result_.start_time}</td>")
            else:
                row.append("<td></td>")
            colspan += 1

            if result_.duration:
                row.append(f"<td>{cls._format_duration(result_.duration)}</td>")
            else:
                row.append("<td></td>")
            colspan += 1

            if result_.urls:
                urls = "<br>".join([cls._get_html_url(url) for url in result_.urls])
                row.append(f"<td>{urls}</td>")
            else:
                row.append("<td></td>")
            colspan += 1

            if sub_html_link:
                row.append(f"<td>{sub_html_link}</td>")
            else:
                row.append("<td></td>")
            colspan += 1

            row.append("</tr>")
            rows_part.append("\n".join(row))
            if result_.info:
                info = escape(result_.info)
                info = (
                    '<tr class="expandable-content">'
                    f'<td colspan="{colspan}"><pre>{info}</pre></td>'
                    "</tr>"
                )
                rows_part.append(info)

        headers = [
            "Name",
            "Status",
            "Start Time",
            "Duration",
            "Urls",
            "Files",
            "Report",
        ]
        headers_html = "".join(["<th>" + h + "</th>" for h in headers])
        results_part = cls.Templates.HTML_TEST_PART.format(
            headers=headers_html, rows="".join(rows_part)
        )

        # TODO: result_.files add to html
        urls = " ".join(
            [
                cls._get_html_url(url)
                for url in sorted(result.urls or [], key=cls._get_html_url_name)
            ]
        )

        html = cls.Templates.HTML_BASE_TEST_TEMPLATE.format(
            title=result.name,
            header=cls._format_header(result.name),
            results=results_part,
            additional_urls=urls,
        )
        report_file = f"{Settings.TEMP_DIR}/{Utils.normalize_string(result.name)}.html"
        Path(report_file).parent.mkdir(parents=True, exist_ok=True)

        with open(report_file, "w", encoding="utf8") as f:
            f.write(html)

        s3_path = f"{Settings.HTML_S3_PATH}/{S3.get_prefix(pr_number=Environment.EventInfo.PR_NUMBER, branch=Environment.BRANCH, sha=Environment.EventInfo.REF_SHA)}"
        sub_html_link = ""
        if upload_to_s3:
            sub_html_link = S3.copy_file_to_s3(s3_path=s3_path, local_path=report_file)

        return HtmlGenerator.HtmlResult(
            result=result,
            html_results=html_results,
            html_file=report_file,
            html_link=sub_html_link,
        )


if __name__ == "__main__":
    result_enclosed = Result(
        name="Test A",
        status=Result.Status.SUCCESS,
        start_time=datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        duration=10,
        results=[],
        urls=[],
        files=[],
        info="context",
    )
    result_top = Result(
        name="Job A",
        status=Result.Status.SUCCESS,
        start_time=datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        duration=10,
        results=[result_enclosed, result_enclosed],
        urls=[],
        files=[],
        info="ok",
    )
    print(
        json.dumps(
            dataclasses.asdict(
                HtmlGenerator.generate_recursive(result_top, upload_to_s3=False)
            ),
            indent=4,
        )
    )
