"""界面配色与样式表。

只用一个浅色主题：学生用户的日常桌面环境，浅色最不容易出问题。
颜色集中在这里，方便以后加暗色主题。
"""

from __future__ import annotations

COLORS = {
    "bg": "#f5f6f8",
    "surface": "#ffffff",
    "border": "#dfe3e8",
    "text": "#1f2933",
    "muted": "#6b7280",
    "accent": "#2f6feb",
    "accent_dark": "#2559c4",
    "ok": "#12805c",
    "warn": "#b45309",
    "error": "#c0392b",
    "drop_bg": "#eef3ff",
    "drop_border": "#9db8f0",
}

STYLESHEET = f"""
QWidget {{
    background: {COLORS['bg']};
    color: {COLORS['text']};
    font-size: 13px;
}}
QMainWindow, QDialog {{ background: {COLORS['bg']}; }}
/* 不给标签单独设透明，它们会继承 QWidget 的底色，看起来像一条灰色的空输入框 */
QLabel {{ background: transparent; }}

QGroupBox {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    margin-top: 18px;
    padding: 10px 10px 10px 10px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 2px 6px;
    color: {COLORS['muted']};
}}

QPushButton {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 6px;
    padding: 5px 12px;
    min-height: 20px;
}}
QPushButton:hover {{ border-color: {COLORS['accent']}; color: {COLORS['accent']}; }}
QPushButton:disabled {{ color: #9aa5b1; border-color: {COLORS['border']}; background: #f0f1f3; }}
QPushButton#primary {{
    background: {COLORS['accent']};
    border-color: {COLORS['accent']};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: {COLORS['accent_dark']}; color: #ffffff; }}
QPushButton#primary:disabled {{ background: #a8c0f0; border-color: #a8c0f0; color: #ffffff; }}
QPushButton#danger:hover {{ border-color: {COLORS['error']}; color: {COLORS['error']}; }}

QListWidget, QTableWidget, QPlainTextEdit, QLineEdit, QComboBox {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 6px;
    selection-background-color: {COLORS['accent']};
    selection-color: #ffffff;
}}
QListWidget::item {{ padding: 4px 2px; }}
QLineEdit, QComboBox {{ padding: 4px 6px; min-height: 20px; }}
QPlainTextEdit {{ font-family: Consolas, "Courier New", monospace; font-size: 12px; }}

QHeaderView::section {{
    background: #f0f2f5;
    border: none;
    border-bottom: 1px solid {COLORS['border']};
    padding: 5px 6px;
    font-weight: 600;
    color: {COLORS['muted']};
}}
QTableWidget {{ gridline-color: #eef0f3; }}
QTableWidget::item {{ padding: 3px 4px; }}

QFrame#dropArea {{
    background: {COLORS['drop_bg']};
    border: 2px dashed {COLORS['drop_border']};
    border-radius: 10px;
}}
QFrame#dropArea[dragActive="true"] {{
    background: #dde8ff;
    border-color: {COLORS['accent']};
}}
QLabel#dropTitle {{ font-size: 15px; font-weight: 600; color: {COLORS['accent_dark']}; }}
QLabel#dropHint {{ color: {COLORS['muted']}; }}
QLabel#hint {{ color: {COLORS['muted']}; }}
QLabel#pill {{
    border-radius: 9px;
    padding: 2px 10px;
    font-weight: 600;
    background: #e8eaee;
    color: {COLORS['muted']};
}}
QLabel#pill[state="ok"] {{ background: #dff3ec; color: {COLORS['ok']}; }}
QLabel#pill[state="busy"] {{ background: #e2ecff; color: {COLORS['accent_dark']}; }}
QLabel#pill[state="error"] {{ background: #fbe3e1; color: {COLORS['error']}; }}
QLabel#pill[state="warn"] {{ background: #fdf0dd; color: {COLORS['warn']}; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #c6ccd4; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: #aab2bd; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QSplitter::handle {{ background: transparent; }}
"""
