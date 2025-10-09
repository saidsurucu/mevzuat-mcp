import subprocess
import sys
from setuptools import setup
from setuptools.command.install import install


class PostInstallCommand(install):
    """Post-installation for installation mode."""
    def run(self):
        install.run(self)
        # Install Playwright browsers
        try:
            print("Installing Playwright browsers...")
            subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
            print("Playwright browsers installed successfully!")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to install Playwright browsers: {e}")
            print("Playwright will still work but may require manual browser installation")


setup(
    cmdclass={
        'install': PostInstallCommand,
    }
)
