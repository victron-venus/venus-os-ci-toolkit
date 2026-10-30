const support = document.querySelector('#support');
const select = document.querySelector('#profile');
const installer = document.querySelector('#installer');
try {
  const response = await fetch('catalog.json', {cache: 'no-cache'});
  if (!response.ok) throw new Error('Release metadata is unavailable.');
  const catalog = await response.json();
  document.querySelector('#release').textContent = `${catalog.version} · ${catalog.channel.toUpperCase()}${catalog.channel === 'stable' ? '' : ' — prerelease'}`;
  document.querySelector('#project').href = catalog.project_url;
  if (catalog.release_url) {
    const release = document.querySelector('#release-notes');
    release.href = catalog.release_url;
    release.hidden = false;
  }
  for (const profile of catalog.profiles) {
    const option = document.createElement('option');
    option.value = profile.id;
    option.textContent = profile.label;
    select.append(option);
  }
  select.disabled = false;
  const supported = window.isSecureContext && 'serial' in navigator;
  let moduleReady = Boolean(customElements.get('esp-web-install-button'));
  const updateSupport = () => {
    if (!supported) {
      support.textContent = 'Installation needs HTTPS and a desktop browser with Web Serial support, such as Chrome or Edge. Downloads remain available.';
    } else if (moduleReady) {
      support.textContent = 'Web Serial is available. Choose the exact hardware profile before connecting.';
    } else {
      support.textContent = 'Loading the USB installer. Firmware downloads remain available.';
    }
    installer.hidden = !(supported && moduleReady && select.value);
  };
  updateSupport();
  const moduleTimeout = window.setTimeout(() => {
    if (supported && !moduleReady) {
      support.textContent = 'The USB installer could not load. Check your connection or content blocker, then reload. Firmware downloads remain available.';
    }
  }, 15000);
  customElements.whenDefined('esp-web-install-button').then(() => {
    moduleReady = true;
    window.clearTimeout(moduleTimeout);
    updateSupport();
  }).catch(() => {
    installer.hidden = true;
    support.textContent = 'The USB installer is unavailable. Firmware downloads remain available.';
  });
  select.addEventListener('change', () => {
    installer.hidden = true;
    installer.removeAttribute('manifest');
    const details = document.querySelector('#details');
    const setup = document.querySelector('#setup');
    const downloads = document.querySelector('#downloads');
    details.replaceChildren();
    setup.replaceChildren();
    downloads.replaceChildren();
    const profile = catalog.profiles.find(item => item.id === select.value);
    if (!profile) return;
    const description = document.createElement('p');
    description.textContent = `${profile.board} · ${profile.chip_family}. ${profile.description || ''}`;
    details.append(description);
    const provisioning = document.createElement('p');
    const setupMessages = {
      improv: 'After installation, use the serial Wi-Fi setup offered by the installer. Application-specific settings may still be required.',
      manual: 'Wi-Fi and application configuration are completed separately in the setup guide.',
      none: 'This firmware does not offer serial Wi-Fi provisioning.',
    };
    provisioning.textContent = setupMessages[profile.provisioning];
    details.append(provisioning);
    if (profile.setup_url) {
      const link = document.createElement('a');
      link.href = profile.setup_url;
      link.textContent = 'Open this hardware profile’s setup guide';
      setup.append(link);
    }
    for (const part of profile.parts) {
      const link = document.createElement('a');
      link.href = part.path;
      link.download = '';
      link.textContent = `Download ${part.path.split('/').pop()} (offset 0x${part.offset.toString(16)})`;
      downloads.append(link, document.createElement('br'));
    }
    installer.setAttribute('manifest', profile.manifest);
    installer.hidden = !(supported && moduleReady);
  });
} catch (error) {
  support.textContent = `Installer unavailable: ${error.message}`;
  select.disabled = true;
  installer.hidden = true;
}
