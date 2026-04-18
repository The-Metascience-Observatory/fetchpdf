from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="fetchpdf",
    version="0.1.0",
    author="Dan Elton",
    author_email="delton17@gmail.com",
    description="A comprehensive tool to download PDFs from DOIs using multiple sources",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/The-Metascience-Observatory/fetchpdf",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Science/Research",
        "Topic :: Scientific/Engineering",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    python_requires=">=3.8",
    install_requires=[
        "requests>=2.31.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "black>=23.0.0",
            "flake8>=6.0.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "fetchpdf=fetchpdf.fetch_pdf_from_doi:main",
        ],
    },
)
