export const handler = function(event: any, context: any) {
  console.log(event);
  // Retrieve user attribute from event request
  const userAttributes = event.request.userAttributes;
  // Add scope to event response
  event.response = {
    claimsAndScopeOverrideDetails: {
      idTokenGeneration: {
        claimsToAddOrOverride: {
          'https://aws.amazon.com/tags': {
            principal_tags: {
              tenantId: [userAttributes['custom:tenantId']],
            },
          },
        },
      },
    },
  };
  // Return to Amazon Cognito
  context.done(null, event);
};