export function GitHubRepositoryProvider(params = {}) {
  return {
    key: 'github',
    name: 'GitHub',
    config: [
      {
        name: 'name',
        label: 'Repository Name',
        type: 'text',
        placeholder: 'e.g. getsentry/sentry',
        help: 'Enter your repository name, including the owner.',
        required: true,
      },
    ],
    ...params,
  };
}
