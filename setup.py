from setuptools import setup, find_packages

setup(
    name="ews-mcp-server",
    version="4.0.0",
    description="MCP Server for Microsoft Exchange Web Services — bidirectional body format, expanded attachment extraction, lean AI surface",
    author="Abdulaziz Almazrou",
    author_email="abdulaziz.almazrou@gmail.com",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    install_requires=[
        "mcp>=1.0.0",
        "exchangelib>=5.0.0",
        "pydantic>=2.5.0",
        "python-dotenv>=1.0.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.4.0",
            "pytest-asyncio>=0.21.0",
            "pytest-cov>=4.1.0",
            "ruff>=0.1.0",
            "mypy>=1.7.0",
            "black>=23.12.0",
        ]
    },
    entry_points={
        "console_scripts": [
            "ews-mcp-server=src.main:main",
        ],
    },
    python_requires=">=3.11",
)
