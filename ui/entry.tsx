import { createRoot, type Root } from 'react-dom/client';
import Workspace, { type View } from './components/workspace';
import stylesheet from './workspace.css?inline';
import theme from './theme.css?inline';

const views = new Set<View>([
  'dashboard', 'negative', 'recommendations', 'projects', 'settings',
  'approvals', 'daily', 'chat', 'audit', 'scheduler',
]);

class DirectAIControl extends HTMLElement {
  private view?: Root;

  connectedCallback() {
    if (this.view) return;
    const shadow = this.shadowRoot ?? this.attachShadow({ mode: 'open' });
    const style = document.createElement('style');
    style.textContent = stylesheet + '\n' + theme;
    const container = document.createElement('div');
    shadow.replaceChildren(style, container);
    this.view = createRoot(container);
    const page = new URLSearchParams(location.search).get('page') as View | null;
    this.view.render(<Workspace initialPage={page && views.has(page) ? page : 'dashboard'} />);
  }

  disconnectedCallback() {
    queueMicrotask(() => {
      if (!this.isConnected) {
        this.view?.unmount();
        this.view = undefined;
      }
    });
  }
}

if (!customElements.get('shtab-direct-ai-control')) {
  customElements.define('shtab-direct-ai-control', DirectAIControl);
}
