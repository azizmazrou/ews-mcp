import Joi from 'joi';

export const schema = Joi.object({
  port: Joi.number().default(3000),
  ewsUrl: Joi.string().uri().required(),
  ewsUsername: Joi.string(),
  ewsPassword: Joi.string(),
});
