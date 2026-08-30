import { defineConfig } from 'vitepress'

const repo = 'https://github.com/timothestoifl24/powerdns'

export default defineConfig({
  title: 'PowerDNS Admin',
  description:
    'An authoritative DNS server you can run with one command: PowerDNS, PostgreSQL and a Tabler admin panel with local, LDAP, OAuth and SAML sign-in.',
  lang: 'en-GB',
  cleanUrls: true,
  lastUpdated: true,
  sitemap: { hostname: 'https://powerdns.stoifl.app' },

  // The panel's own URLs are meant to be unreachable from a build machine.
  ignoreDeadLinks: [/^https?:\/\/(localhost|127\.0\.0\.1)/],

  head: [
    ['link', { rel: 'icon', type: 'image/svg+xml', href: '/favicon.svg' }],
    ['meta', { name: 'theme-color', content: '#0054a6' }],
    ['meta', { property: 'og:type', content: 'website' }],
    ['meta', { property: 'og:site_name', content: 'PowerDNS Admin' }],
    ['meta', { property: 'og:url', content: 'https://powerdns.stoifl.app/' }],
    [
      'meta',
      {
        property: 'og:description',
        content:
          'PowerDNS Authoritative + PostgreSQL + a Tabler admin panel, wired together in one compose file.',
      },
    ],
  ],

  themeConfig: {
    logo: '/favicon.svg',

    nav: [
      { text: 'Guide', link: '/guide' },
      { text: 'Setup', link: '/setup' },
      { text: 'Screenshots', link: '/screenshots' },
      {
        text: 'Reference',
        items: [
          { text: 'Advanced configuration', link: '/advanced-config' },
          { text: 'Upgrading', link: '/upgrading' },
          { text: 'FAQ', link: '/faq' },
        ],
      },
      { text: 'ghcr.io images', link: 'https://github.com/timothestoifl24?tab=packages' },
    ],

    sidebar: [
      {
        text: 'Getting started',
        items: [
          { text: 'Overview', link: '/' },
          { text: 'Guide', link: '/guide' },
          { text: 'Setup', link: '/setup' },
          { text: 'Screenshots', link: '/screenshots' },
        ],
      },
      {
        text: 'Running it',
        items: [
          { text: 'Advanced configuration', link: '/advanced-config' },
          { text: 'Upgrading', link: '/upgrading' },
          { text: 'FAQ', link: '/faq' },
        ],
      },
    ],

    socialLinks: [{ icon: 'github', link: repo }],

    editLink: {
      pattern: `${repo}/edit/main/docs/:path`,
      text: 'Edit this page on GitHub',
    },

    search: { provider: 'local' },

    outline: { level: [2, 3] },

    footer: {
      message: 'Released under the GPL-3.0 licence.',
      copyright: `© ${new Date().getFullYear()} Timothé Stoifl`,
    },
  },
})
